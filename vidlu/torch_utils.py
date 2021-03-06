import contextlib
import typing as T

import torch
from torch import nn

import torch.utils.checkpoint as tuc


def is_float_tensor(x):
    return 'float' in str(x.dtype)


def is_int_tensor(x):
    return 'int' in str(x.dtype)


def round_float_to_int(x, dtype=torch.int):
    return x.sign().mul_(0.5).add_(x).to(dtype)


# General context managers (move out of utils/torch?

@contextlib.contextmanager
def switch_attribute(objects, attrib_name, value):
    state = {m: getattr(m, attrib_name) for m in objects}
    for m in state:
        setattr(m, attrib_name, value)
    yield
    for m, v in state.items():
        setattr(m, attrib_name, v)


@contextlib.contextmanager
def switch_attributes(objects, **name_to_value):
    with contextlib.ExitStack() as stack:
        for name, value in name_to_value.items():
            stack.enter_context(switch_attribute(objects, name, value))
        yield


def switch_attribute_if_exists(objects, attrib_name, value):
    return switch_attribute((k for k in objects if hasattr(k, attrib_name)), attrib_name, value)


@contextlib.contextmanager
def save_attribute(objects, attrib_name, copy_func=lambda x: x):
    state = {m: copy_func(getattr(m, attrib_name)) for m in objects}
    yield
    for m, v in state.items():
        setattr(m, attrib_name, v)


# Context managers for modules and tensors

def save_tensor_attribute(objects, attrib_name):
    return save_attribute(objects, attrib_name, lambda x: x.detach().clone())


def switch_requires_grad(module_or_params, value):
    params = module_or_params.parameters() if isinstance(module_or_params, torch.nn.Module) \
        else module_or_params
    return switch_attribute(params, 'requires_grad', value)


def switch_training(module, value):
    return switch_attribute(module.modules(), 'training', value)


def switch_batchnorm_momentum(module, value):
    modules = (m for m in module.modules()
               if type(m).__name__.startswith('BatchNorm') and hasattr(m, 'momentum'))
    return switch_attribute(modules, 'momentum', value)


@contextlib.contextmanager
def batchnorm_stats_tracking_off(module):
    modules = [m for m in module.modules()
               if type(m).__name__.startswith('BatchNorm') and hasattr(m, 'momentum')]
    with switch_attribute(modules, 'momentum', 0), \
         save_tensor_attribute(modules, 'num_batches_tracked'):
        yield


@contextlib.contextmanager
def save_grads(params):
    param_grads = [(p, p.grad.detach().clone()) for p in params]
    yield param_grads
    for p, g in param_grads:
        p.grad = g


@contextlib.contextmanager
def save_params(params):
    param_param_saved = [(p, p.detach().clone()) for p in params]
    yield param_param_saved
    with torch.no_grad():
        for p, p_saved in param_param_saved:
            p.data = p_saved


# tensor trees

def concatenate_tensors_trees(*args, tree_type=None):
    """Concatenates corresponding arrays from equivalent (parallel) trees.

    Example:
        >>> x1, y1, z1, x2, y2, z2 = [torch.tensor([i]) for i in range(6)]
        >>> cat = lambda x, y: torch.cat([x, y], dim=0)
        >>> a = dict(q=dict(x=x1, y=y1), z=z1)
        >>> b = dict(q=dict(x=x2, y=y2), z=z2)
        >>> c1 = concatenate_tensors_trees(a, b)
        >>> c2 = dict(q=dict(x=cat(x1, x2), y=cat(y1, y2)), z=cat(z1, z2))
        >>> assert c1 == c2

    Args:
        *args: trees with Torch arrays as leaves.
        tree_type (optional): A dictionary type used to recognize what a
            subtree is if they are of mixed types (e.g. `dict` and some other
            dictionary type that is not a subtype od `dict`).

    Returns:
        A tree with concatenated arrays
    """
    tree_type = tree_type or type(args[0])
    keys = args[0].keys()
    values = []
    for k in keys:
        vals = [a[k] for a in args]
        if isinstance(vals[0], tree_type):
            values.append(concatenate_tensors_trees(*vals, tree_type=tree_type))
        else:
            values.append(torch.cat(vals, dim=0))
    return tree_type(zip(keys, values))


# Autograd checkpointing

def is_modified(tensor: torch.Tensor):
    return tensor._version > 0


def checkpoint_fix(function, *args, **kwargs):
    """A workaround for CheckpointFunctionBackward not being set (and called)
    when the output of `function` is an in-place modified view tensor, e.g
    `x[:].relu_()`.
    """

    def wrapper(*args_, **kwargs_):
        result = function(*args_, **kwargs_)
        if isinstance(result, torch.Tensor):
            return result[...] if is_modified(result) else result
        elif isinstance(result, T.Sequence):
            return type(result)(r[...] if is_modified(r) else r for r in result)
        elif isinstance(result, T.Mapping):
            return type(result)({k: r[...] if is_modified(r) else r for k, r in result.items()})
        else:
            raise NotImplementedError()

    return tuc.checkpoint(wrapper, *args, **kwargs)


class StateAwareCheckpoint:
    def __init__(self, module, context_managers_fs=(batchnorm_stats_tracking_off,)):
        self.context_managers_fs = context_managers_fs
        self.module = module if isinstance(module, nn.Module) else nn.ModuleList(module)

    def __call__(self, func, *args, **kwargs):
        with contextlib.ExitStack() as stack:
            for cmf in self.context_managers_fs:
                stack.enter_context(cmf(self.module))
            return checkpoint_fix(func, *args, **kwargs)


# Cuda memory management

def reset_cuda():
    torch.cuda.empty_cache()
    torch.cuda.ipc_collect()


# Profiling

def profile(func, on_cuda=True, device=None):
    with torch.autograd.profiler.profile(use_cuda=on_cuda) as prof:
        output = func()
    if on_cuda:
        torch.cuda.synchronize(device=device)
    return output, prof.key_averages().table('cuda_time_total')
