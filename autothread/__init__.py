# Copyright 2022 by Bas de Bruijne
# All rights reserved.
# autothread comes with ABSOLUTELY NO WARRANTY, the writer can not be
# held responsible for any problems caused by the use of this module.

__author__ = "Bas de Bruijne"
__version__ = "0.0.6"

import ctypes
import functools
import inspect
import multiprocess as mp
import os
import psutil
import queue
import signal
import threading
import typeguard
import warnings

from tqdm import tqdm
from typing import List, Union, Optional, Tuple, Dict, Callable, Type


def _queuer(
    queue: Union[queue.Queue, mp.Queue],
    function: Callable,
    semaphore: Union[threading.Semaphore, mp.Semaphore],
    index: int,
    *args,
    **kwargs,
):
    """Function wrapper to put the outputs in the queue

    The queuer is kept outside of the _Autothread class such that multiprocess doesn't
    have to pickle/dill the entire class.
    :param queue: Queue object to return values to
    :param function: function to forward *args and **kwrags to
    :param semaphore: Semaphore object (to limit the number of concurrent workers)
    :param index: Index to track when this thread was started
    """
    try:
        output = function(*args, **kwargs)
    except KeyboardInterrupt:
        return
    except Exception as e:
        e.autothread_intercepted = True
        output = e
    semaphore.release()
    queue.put({index: output})


class _Autothread:
    """Decorator class that transforms a function into a multi processed
    function simply by adding a single decorator."""

    def __init__(
        self,
        function: Callable,
        Process: Union[Type[threading.Thread], Type[mp.Process]],
        Queue: Union[Type[queue.Queue], Type[mp.Queue]],
        Semaphore: Union[Type[threading.Semaphore], Type[mp.Semaphore]],
        n_workers: int,
        progress_bar: bool,
    ):
        """Initialize the decorator

        :param function: function to decorate
        :param Process: Process/thread class
        :param Queue: Queue class
        :param Semaphore: Semaphore class
        :param n_workers: Total number of workers to use
        :oaram progress_bar: Whether to show a progress bar
        """
        self._Process = Process
        self._Queue = Queue
        self._Semaphore = Semaphore
        self._function = function
        self.n_workers = n_workers
        self._params = inspect.signature(self._function).parameters
        self._progress_bar = progress_bar
        self._is_listy = lambda x: isinstance(x, list) or isinstance(x, tuple)

    @property
    def __signature__(self):
        """Updates the __signature__ to match the received function"""
        signature = inspect.signature(self._function)
        new_params = []
        for k in self._params:
            param = self._params[k]
            if param.__str__().startswith("*"):
                if not param.__str__().startswith("**"):
                    new_params.append(param)
                continue
            new_params.append(
                inspect.Parameter(
                    name=k,
                    kind=param.kind,
                    default=param.default,
                    annotation=Union[List[param.annotation], param.annotation],
                )
            )
        new_params.append(
            inspect.Parameter(
                name="_loop_params",
                kind=inspect._ParameterKind(3),
                default=None,
                annotation=Optional[List[str]],
            )
        )
        for k in self._params:
            if self._params[k].__str__().startswith("**"):
                new_params.append(self._params[k])

        return signature.replace(parameters=new_params)

    @property
    def __doc__(self):
        """Updates the __doc__ to match the received function"""
        if self._function.__doc__:
            return self._function.__doc__ + (
                "\n This function is automatically parallelized using autothread. Any "
                "of this function's arguments can be substituted with a list and this "
                "function will be repeated for each item in that list."
            )

    def __call__(self, *args, **kwargs):
        """Call the function

        :param args: Arguments to forward to the function
        :param kwargs: Keyword argumented to forward
        """
        self._setup(args, kwargs)

        if not self._loop_params:  # just run the function as normal
            return self._function(*args, **kwargs)

        results = {}
        for i, args, kwargs in self._contruct_args():
            self._sema.acquire()
            p = self._Process(target=_queuer, args=args, kwargs=self._extra_kwargs)
            p.start()
            self._processes.append(p)
            if i >= self.n_workers and self.n_workers >= 0:
                results.update(self._collect_result())

        while self._processes:
            results.update(self._collect_result())

        if self._progress_bar:
            self._tqdm.close()

        return [v[1] for v in sorted(results.items())]

    def _setup(self, args: Tuple, kwargs: Dict):
        """Setup the multiprocessing variables and arguments

        :param args: Arguments to forward to the function
        :param kwargs: Keyword argumented to forward
        """
        self._queue = self._Queue()
        self._processes = []
        self._sema = self._Semaphore(self.n_workers if self.n_workers > 0 else int(1e9))
        self._loop_params = kwargs.pop("_loop_params", [])
        self._merge_args(args, kwargs)
        self._loop_params = self._get_loop_params(self._loop_params)
        self._verify_loop_params(self._loop_params)

    def _merge_args(self, args: Tuple, kwargs: Dict):
        """Merge args into kwargs

        Since we know the keyword names, it it easier to keep track of only the kwargs
        by merging the args into it. The *arg and **kwargs present in the original
        function cannot be captured here, we leave those in self._extra_(kw)args.

        :param args: List of arguments
        :param kwargs: Dict of keyword arguments
        """
        self._arg_names = list(self._params.keys())
        self._kwargs = {}
        self._extra_args = []
        self._extra_kwargs = {}
        for i, v in enumerate(args):
            if self._params[self._arg_names[i]].__str__().startswith("*"):
                self._extra_args = args[i:]
                break
            self._kwargs[self._arg_names[i]] = {"value": v, "is_kwarg": False}
        for k, v in kwargs.items():
            if not (k in self._params or k in self._loop_params):
                self._extra_kwargs[k] = v
            else:
                self._kwargs[k] = {"value": v, "is_kwarg": True}

        self._arg_lengths = {}
        for k, v in self._kwargs.items():
            length = len(v["value"]) if self._is_listy(v["value"]) else None
            self._arg_lengths[k] = length

    def _get_loop_params(self, loop_params: Optional[List[str]]):
        """Determine which arguments will be split into the different threads

        If '_loop_params' is not defined, we will determine which parameters should be
        split into the threads by checking the values against their type hints. If a
        list is provided whose contents match the original type hint, we will assume
        that this parameter needs to be split into the separate threads.

        :param loop_params: Override list of loop_parameters provided by user
        """
        if loop_params:
            if not self._is_listy(loop_params):
                loop_params = [loop_params]
            return loop_params

        _type_warning = (
            "Type hint for {k} could not be verified. Even though it is a list, it will"
            " not be used for parallelization. It is possible that the type hints of "
            "this function are incorrect. If correct, use the `_loop_params` keyword "
            "argument to specify the parameters to parallelize for."
        )
        loop_params = []
        for k, v in self._kwargs.items():
            if not k in self._params and self._is_listy(v["value"]):
                warnings.warn(_type_warning.format(k=k))
                continue
            type_hint = self._params[k].annotation
            if self._checks_type(v["value"], type_hint):
                continue
            elif self._is_listy(v["value"]) and all(
                self._checks_type(_v, type_hint) for _v in v["value"]
            ):
                loop_params.append(k)
            elif self._is_listy(v["value"]):
                warnings.warn(_type_warning.format(k=k))
            # else: Type hint is incorrect but not list-y, we will just ignore it
        for k, v in self._extra_kwargs.items():
            if self._is_listy(v):
                warnings.warn(_type_warning.format(k=k))
        return loop_params

    def _verify_loop_params(self, loop_params: List[str]):
        """Make sure that the loop params are valid

        The loop parameters are valid if:
        1) They are all provided by the user
        2) They are all of the same length

        :param loop_params: List of loop_parameters provided by _get_loop_params
        """
        for param in loop_params:
            if not (param in self._kwargs or param in self._extra_kwargs):
                raise ValueError(
                    f"'{param}' is specified as loop parameter but does not "
                    "exist for this function or is not provided. Choose one "
                    f"of {list(self._kwargs)}"
                )
        if any(
            self._arg_lengths[param] != self._arg_lengths[loop_params[0]]
            for param in loop_params
        ):
            raise IndexError(
                f"Input for parallelization is ambiguous. {loop_params} are "
                "all lists but are of different lengths. It is possible that the type "
                "hints of this function are incorrect. If they are not, use the "
                "`_loop_params` keyword argument to specify the parameters to "
                "parallelize for."
            )
        return loop_params

    def _contruct_args(self):
        """Contruct arguments and keyword arguments for each thread/process

        For each argument, extract an item if it is in the loop_params or just select
        the value and put them in tuples and dicts to forward to the function.
        """
        n_threads = self._arg_lengths[self._loop_params[0]]

        if self._progress_bar:
            self._tqdm = tqdm(total=n_threads)

        for i in range(n_threads):
            args = [self._queue, self._function, self._sema, i]
            for k, v in self._kwargs.items():
                value = v["value"][i] if k in self._loop_params else v["value"]
                if v["is_kwarg"]:
                    self._extra_kwargs[k] = value
                else:
                    args.append(value)
            args.extend(self._extra_args)

            yield i, args, self._extra_kwargs

    def _checks_type(self, value, type_hint):
        """Check if a value corresponds to a type hint

        :param value: Value to check type hint for
        :param type_hint: Type hint to validate
        """
        try:
            typeguard.check_type("foo", value, type_hint)
            return True
        except TypeError:
            return False

    def _collect_result(self):
        """Collect the results from the queue and raise possible errors

        The queue does not return items in order if the processing times are different
        for different parameters. The queue will return (N, output) where N is its
        original place in the queue that must be sorted.
        """
        while self._processes:
            for process in self._processes:
                if process.is_alive():
                    continue

                process.join()
                self._processes.remove(process)

                if self._progress_bar:
                    self._tqdm.update(1)

                res = self._queue.get()
                content = list(res.values())[0]
                if isinstance(content, Exception) and getattr(
                    content, "autothread_intercepted", False
                ):
                    try:
                        self._kill_all()
                    except KeyboardInterrupt:
                        # The main thread can accidentally be killed on some platforms
                        pass
                    raise content
                return res

    def _kill_all(self):
        """Terminates all running processes by sending them a keyboard interrupt"""
        if self._Process == threading.Thread:
            p_names = [p.name for p in self._processes]
            for id, thread in threading._active.copy().items():
                if thread.name in p_names:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_long(id),
                        ctypes.py_object(KeyboardInterrupt),
                    )
        else:
            for process in self._processes:
                os.kill(process.pid, getattr(signal, "CTRL_C_EVENT", signal.SIGINT))
        for process in self._processes:
            process.join()


class Multithreaded:
    """Decorator to make any function multithreaded

    This decorator will allow any function to receive a list where it would initially
    receive single items. The function will be repeated for every item in that list in
    parallel and the results will be concatenated into a list and returned back.
    """

    Process = threading.Thread
    Queue = queue.Queue
    Semaphore = threading.Semaphore

    def __init__(
        self,
        n_workers: int = None,
        mb_mem: int = None,
        workers_per_core: int = None,
        progress_bar: bool = False,
    ):
        """Initialize the autothread decorator

        :param n_workers: Total number of workers to run in parallel (0 for unlimited,
        (default) None for the amount of cores).
        :param mb_mem: Minimum megabytes of memory for each worker.
        :param workers_per_core: Number of workers to run per core.
        :param progress_bar: Visualize how many of the tasks are completed
        """
        self.n_workers = self._get_workers(n_workers, mb_mem, workers_per_core)
        self.process_bar = progress_bar

    def __call__(self, function: Callable, *args, **kwargs):
        if not callable(function):
            raise SyntaxError(
                f"{self.__class__.__name__} did not receive a function, but a "
                f"{str(type(function))}."
                f"\n@autothread.{self.__class__.__name__}() <- Did you forget the ()?"
                f"\n{' '*(len(self.__class__.__name__)+12)}~~"
            )

        decorator = _Autothread(
            function=function,
            Process=self.Process,
            Queue=self.Queue,
            Semaphore=self.Semaphore,
            n_workers=self.n_workers,
            progress_bar=self.process_bar,
        )

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            return decorator(*args, **kwargs)

        wrapper.__doc__ = decorator.__doc__
        wrapper.__signature__ = decorator.__signature__

        return wrapper

    def _get_workers(self, *args):
        """Determined the number of workers to use based on the users inputs

        :param n_workers: Total number of workers to run in parallel (0 for unlimited,
        (default) None for the amount of cores).
        :param mb_mem: Minimum megabytes of memory for each worker.
        :workers_per_core: Number of workers to run per core.
        """
        if sum(not arg is None for arg in args) > 1:
            raise ValueError(
                "Please only define one of 'n_workers', 'mb_mem', 'or workers_per_core'"
            )
        n_workers, mb_mem, workers_per_core = args
        if mb_mem:
            return int(psutil.virtual_memory().total / 1024**2 // mb_mem)
        elif workers_per_core:
            return int(workers_per_core * mp.cpu_count())
        elif n_workers is None:
            return mp.cpu_count()
        else:
            return n_workers


class Multiprocessed(Multithreaded):
    """Decorator to make any function multiprocessed

    This decorator will allow any function to receive a list where it would initially
    receive single items. The function will be repeated for every item in that list in
    parallel and the results will be concatenated into a list and returned back.
    """

    Process = mp.Process
    Queue = mp.Queue
    Semaphore = mp.Semaphore


multithreaded = Multithreaded
multiprocessed = Multiprocessed
