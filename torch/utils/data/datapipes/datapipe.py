import functools
import pickle
from typing import Dict, Callable, Optional, TypeVar, Generic, Iterator

from torch.utils.data.datapipes._typing import _DataPipeMeta, _IterDataPipeMeta
from torch.utils.data.dataset import Dataset, IterableDataset

try:
    import dill
    # XXX: By default, dill writes the Pickler dispatch table to inject its
    # own logic there. This globally affects the behavior of the standard library
    # pickler for any user who transitively depends on this module!
    # Undo this extension to avoid altering the behavior of the pickler globally.
    dill.extend(use_dill=False)
    HAS_DILL = True
except ImportError:
    HAS_DILL = False

__all__ = [
    "DataChunk",
    "DFIterDataPipe",
    "IterDataPipe",
    "MapDataPipe",
]

T = TypeVar('T')
T_co = TypeVar('T_co', covariant=True)

UNTRACABLE_DATAFRAME_PIPES = ['batch',  # As it returns DataChunks
                              'groupby',   # As it returns DataChunks
                              '_dataframes_as_tuples',  # As it unpacks DF
                              'trace_as_dataframe',  # As it used to mark DF for tracing
                              ]


class IterDataPipe(IterableDataset[T_co], metaclass=_IterDataPipeMeta):
    r"""
    Iterable-style DataPipe.

    All DataPipes that represent an iterable of data samples should subclass this.
    This style of DataPipes is particularly useful when data come from a stream, or
    when the number of samples is too large to fit them all in memory. ``IterDataPipe`` is lazily initialized and its
    elements are computed only when ``next()`` is called on the iterator of an ``IterDataPipe``.

    All subclasses should overwrite :meth:`__iter__`, which would return an
    iterator of samples in this DataPipe. Calling ``__iter__`` of an ``IterDataPipe`` automatically invokes its
    method ``reset()``, which by default performs no operation. When writing a custom ``IterDataPipe``, users should
    override ``reset()`` if necessary. The common usages include resetting buffers, pointers,
    and various state variables within the custom ``IterDataPipe``.

    Note:
        Only `one` iterator can be valid for each ``IterDataPipe`` at a time,
        and the creation a second iterator will invalidate the first one. This constraint is necessary because
        some ``IterDataPipe`` have internal buffers, whose states can become invalid if there are multiple iterators.
        The code example below presents details on how this constraint looks in practice.
        If you have any feedback related to this constraint, please see `GitHub IterDataPipe Single Iterator Issue`_.

    These DataPipes can be invoked in two ways, using the class constructor or applying their
    functional form onto an existing ``IterDataPipe`` (recommended, available to most but not all DataPipes).
    You can chain multiple `IterDataPipe` together to form a pipeline that will perform multiple
    operations in succession.

    .. _GitHub IterDataPipe Single Iterator Issue:
        https://github.com/pytorch/data/issues/45

    Note:
        When a subclass is used with :class:`~torch.utils.data.DataLoader`, each
        item in the DataPipe will be yielded from the :class:`~torch.utils.data.DataLoader`
        iterator. When :attr:`num_workers > 0`, each worker process will have a
        different copy of the DataPipe object, so it is often desired to configure
        each copy independently to avoid having duplicate data returned from the
        workers. :func:`~torch.utils.data.get_worker_info`, when called in a worker
        process, returns information about the worker. It can be used in either the
        dataset's :meth:`__iter__` method or the :class:`~torch.utils.data.DataLoader` 's
        :attr:`worker_init_fn` option to modify each copy's behavior.

    Examples:
        General Usage:
            >>> from torchdata.datapipes.iter import IterableWrapper, Mapper
            >>> dp = IterableWrapper(range(10))
            >>> map_dp_1 = Mapper(dp, lambda x: x + 1)  # Using class constructor
            >>> map_dp_2 = dp.map(lambda x: x + 1)  # Using functional form (recommended)
            >>> list(map_dp_1)
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            >>> list(map_dp_2)
            [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            >>> filter_dp = map_dp_1.filter(lambda x: x % 2 == 0)
            >>> list(filter_dp)
            [2, 4, 6, 8, 10]
        Single Iterator Constraint Example:
            >>> from torchdata.datapipes.iter import IterableWrapper, Mapper
            >>> dp = IterableWrapper(range(10))
            >>> it1 = iter(source_dp)
            >>> list(it1)
            [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
            >>> it1 = iter(source_dp)
            >>> it2 = iter(source_dp)  # The creation of a new iterator invalidates `it1`
            >>> next(it2)
            0
            >>> next(it1)  # Further usage of `it1` will raise a `RunTimeError`
    """
    functions: Dict[str, Callable] = {}
    reduce_ex_hook: Optional[Callable] = None
    getstate_hook: Optional[Callable] = None
    str_hook: Optional[Callable] = None
    repr_hook: Optional[Callable] = None
    _valid_iterator_id: Optional[int] = None
    _restored: bool = False

    def __getattr__(self, attribute_name):
        if attribute_name in IterDataPipe.functions:
            function = functools.partial(IterDataPipe.functions[attribute_name], self)
            return function
        else:
            raise AttributeError("'{0}' object has no attribute '{1}".format(self.__class__.__name__, attribute_name))

    @classmethod
    def register_function(cls, function_name, function):
        cls.functions[function_name] = function

    @classmethod
    def register_datapipe_as_function(cls, function_name, cls_to_register, enable_df_api_tracing=False):
        if function_name in cls.functions:
            raise Exception("Unable to add DataPipe function name {} as it is already taken".format(function_name))

        def class_function(cls, enable_df_api_tracing, source_dp, *args, **kwargs):
            result_pipe = cls(source_dp, *args, **kwargs)
            if isinstance(result_pipe, IterDataPipe):
                if enable_df_api_tracing or isinstance(source_dp, DFIterDataPipe):
                    if function_name not in UNTRACABLE_DATAFRAME_PIPES:
                        result_pipe = result_pipe.trace_as_dataframe()

            return result_pipe

        function = functools.partial(class_function, cls_to_register, enable_df_api_tracing)
        cls.functions[function_name] = function

    def __getstate__(self):
        """
        This contains special logic to serialize `lambda` functions when `dill` is available.
        If this doesn't cover your custom DataPipe's use case, consider writing custom methods for
        `__getstate__` and `__setstate__`, or use `pickle.dumps` for serialization.
        """
        if IterDataPipe.getstate_hook is not None:
            return IterDataPipe.getstate_hook(self)
        return self.__dict__

    def __reduce_ex__(self, *args, **kwargs):
        if IterDataPipe.reduce_ex_hook is not None:
            try:
                return IterDataPipe.reduce_ex_hook(self)
            except NotImplementedError:
                pass
        return super().__reduce_ex__(*args, **kwargs)

    @classmethod
    def set_getstate_hook(cls, hook_fn):
        if IterDataPipe.getstate_hook is not None and hook_fn is not None:
            raise Exception("Attempt to override existing getstate_hook")
        IterDataPipe.getstate_hook = hook_fn

    @classmethod
    def set_reduce_ex_hook(cls, hook_fn):
        if IterDataPipe.reduce_ex_hook is not None and hook_fn is not None:
            raise Exception("Attempt to override existing reduce_ex_hook")
        IterDataPipe.reduce_ex_hook = hook_fn

    def __repr__(self):
        if self.repr_hook is not None:
            return self.repr_hook(self)
        # Instead of showing <torch. ... .MapperIterDataPipe object at 0x.....>, return the class name
        return str(self.__class__.__qualname__)

    def __str__(self):
        if self.str_hook is not None:
            return self.str_hook(self)
        # Instead of showing <torch. ... .MapperIterDataPipe object at 0x.....>, return the class name
        return str(self.__class__.__qualname__)

    def reset(self):
        r"""
        Reset the `IterDataPipe` to the initial state. By default, no-op. For subclasses of `IterDataPipe`,
        depending on their functionalities, they may want to override this method with implementations that
        may clear the buffers and reset pointers of the DataPipe.
        The `reset` method is always called when `__iter__` is called as part of `hook_iterator`.
        """
        pass


class DFIterDataPipe(IterDataPipe):
    def _is_dfpipe(self):
        return True


class MapDataPipe(Dataset[T_co], metaclass=_DataPipeMeta):
    r"""
    Map-style DataPipe.

    All datasets that represent a map from keys to data samples should subclass this.
    Subclasses should overwrite :meth:`__getitem__`, supporting fetching a
    data sample for a given, unique key. Subclasses can also optionally overwrite
    :meth:`__len__`, which is expected to return the size of the dataset by many
    :class:`~torch.utils.data.Sampler` implementations and the default options
    of :class:`~torch.utils.data.DataLoader`.

    These DataPipes can be invoked in two ways, using the class constructor or applying their
    functional form onto an existing `MapDataPipe` (recommend, available to most but not all DataPipes).

    Note:
        :class:`~torch.utils.data.DataLoader` by default constructs an index
        sampler that yields integral indices. To make it work with a map-style
        DataPipe with non-integral indices/keys, a custom sampler must be provided.

    Example:
        >>> from torchdata.datapipes.map import SequenceWrapper, Mapper
        >>> dp = SequenceWrapper(range(10))
        >>> map_dp_1 = dp.map(lambda x: x + 1)  # Using functional form (recommended)
        >>> list(map_dp_1)
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        >>> map_dp_2 = Mapper(dp, lambda x: x + 1)  # Using class constructor
        >>> list(map_dp_2)
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        >>> batch_dp = map_dp_1.batch(batch_size=2)
        >>> list(batch_dp)
        [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]
    """
    functions: Dict[str, Callable] = {}
    reduce_ex_hook: Optional[Callable] = None
    getstate_hook: Optional[Callable] = None
    str_hook: Optional[Callable] = None
    repr_hook: Optional[Callable] = None

    def __getattr__(self, attribute_name):
        if attribute_name in MapDataPipe.functions:
            function = functools.partial(MapDataPipe.functions[attribute_name], self)
            return function
        else:
            raise AttributeError("'{0}' object has no attribute '{1}".format(self.__class__.__name__, attribute_name))

    @classmethod
    def register_function(cls, function_name, function):
        cls.functions[function_name] = function

    @classmethod
    def register_datapipe_as_function(cls, function_name, cls_to_register):
        if function_name in cls.functions:
            raise Exception("Unable to add DataPipe function name {} as it is already taken".format(function_name))

        def class_function(cls, source_dp, *args, **kwargs):
            result_pipe = cls(source_dp, *args, **kwargs)
            return result_pipe

        function = functools.partial(class_function, cls_to_register)
        cls.functions[function_name] = function

    def __getstate__(self):
        """
        This contains special logic to serialize `lambda` functions when `dill` is available.
        If this doesn't cover your custom DataPipe's use case, consider writing custom methods for
        `__getstate__` and `__setstate__`, or use `pickle.dumps` for serialization.
        """
        if MapDataPipe.getstate_hook is not None:
            return MapDataPipe.getstate_hook(self)
        return self.__dict__

    def __reduce_ex__(self, *args, **kwargs):
        if MapDataPipe.reduce_ex_hook is not None:
            try:
                return MapDataPipe.reduce_ex_hook(self)
            except NotImplementedError:
                pass
        return super().__reduce_ex__(*args, **kwargs)

    @classmethod
    def set_getstate_hook(cls, hook_fn):
        if MapDataPipe.getstate_hook is not None and hook_fn is not None:
            raise Exception("Attempt to override existing getstate_hook")
        MapDataPipe.getstate_hook = hook_fn

    @classmethod
    def set_reduce_ex_hook(cls, hook_fn):
        if MapDataPipe.reduce_ex_hook is not None and hook_fn is not None:
            raise Exception("Attempt to override existing reduce_ex_hook")
        MapDataPipe.reduce_ex_hook = hook_fn

    def __repr__(self):
        if self.repr_hook is not None:
            return self.repr_hook(self)
        # Instead of showing <torch. ... .MapperMapDataPipe object at 0x.....>, return the class name
        return str(self.__class__.__qualname__)

    def __str__(self):
        if self.str_hook is not None:
            return self.str_hook(self)
        # Instead of showing <torch. ... .MapperMapDataPipe object at 0x.....>, return the class name
        return str(self.__class__.__qualname__)


class _DataPipeSerializationWrapper:
    def __init__(self, datapipe):
        self._datapipe = datapipe

    def __getstate__(self):
        use_dill = False
        try:
            value = pickle.dumps(self._datapipe)
        except Exception:
            if HAS_DILL:
                value = dill.dumps(self._datapipe)
                use_dill = True
            else:
                raise
        return (value, use_dill)

    def __setstate__(self, state):
        value, use_dill = state
        if use_dill:
            self._datapipe = dill.loads(value)
        else:
            self._datapipe = pickle.loads(value)

    def __len__(self):
        try:
            return len(self._datapipe)
        except Exception:
            raise TypeError(
                "{} instance doesn't have valid length".format(type(self).__name__)
            )


class _IterDataPipeSerializationWrapper(_DataPipeSerializationWrapper, IterDataPipe):
    def __iter__(self):
        yield from self._datapipe


class _MapDataPipeSerializationWrapper(_DataPipeSerializationWrapper, MapDataPipe):
    def __getitem__(self, idx):
        return self._datapipe[idx]


class DataChunk(list, Generic[T]):
    def __init__(self, items):
        super().__init__(items)
        self.items = items

    def as_str(self, indent=''):
        res = indent + "[" + ", ".join(str(i) for i in iter(self)) + "]"
        return res

    def __iter__(self) -> Iterator[T]:
        for i in super().__iter__():
            yield i

    def raw_iterator(self) -> T:  # type: ignore[misc]
        for i in self.items:
            yield i
