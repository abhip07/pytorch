import torch
import contextlib
from typing import Iterator
from torch.utils._pytree import tree_map
from functools import partial
from torch.utils._python_dispatch import enable_python_mode

# TODO: move this into library proper
@contextlib.contextmanager
def no_dispatch() -> Iterator[None]:
    guard = torch._C._DisableTorchDispatch()  # type: ignore[attr-defined]
    try:
        yield
    finally:
        del guard

def check_metadata_consistency(wrapper_tensor):
    if not isinstance(wrapper_tensor, CompositeCompliantTensor):
        return
    elem = wrapper_tensor.elem
    if wrapper_tensor.shape != elem.shape:
        raise RuntimeError(
            "This operator is not CompositeImplicitAutograd compliant: the "
            "shape of the tensor was modified directly without "
            "going through the PyTorch dispatcher.")
    if wrapper_tensor.dtype != elem.dtype:
        raise RuntimeError(
            "This operator is not CompositeImplicitAutograd compliant: the "
            "dtype of the tensor was modified directly without "
            "going through the PyTorch dispatcher.")

# This is a bit fragile because this needs to be updated whenever a new view op
# is added. We probably want some sort of introspection to tell that if a
# torch.* op is a view or not.
def is_view_fn(func):
    return func.__name__ in {
        'as_strided',
        'detach',
        'diagonal',
        'expand',
        'expand_as',
        'movedim',
        'narrow',
        'permute',
        'select',
        'squeeze',
        'transpose',
        't',
        'real',
        'imag',
        'view_as_real',
        'view_as_complex',
        'unflatten',
        'unfold',
        'unsqueeze',
        'view',
        'view_as',
        'unbind',
        'split',
        'split_with_sizes',
        'vsplit',
        'hsplit',
        'tensor_split',
        'chunk',
        'swapaxes',
        'slice',
        '_reshape_alias',
        '_unsafe_view',
        '_conj',
    }

def is_inplace_view_fn(func):
    return func.__name__ in {
        'squeeze_',
        'unsqueeze_',
        'transpose_',
        't_',
    }

class CompositeCompliantTensor(torch.Tensor):
    elem: torch.Tensor

    __slots__ = ['elem']

    @staticmethod
    def __new__(cls, elem, *args, **kwargs):
        # The storage of CompositeCompliantTensor should never be used directly
        # by a CompositeImplicitAutograd operation; if the CompositeImplicitAutograd
        # operator attempts to read from the storage without dispatching then it'll
        # raise a RuntimeError due to it being a meta storage.
        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls, elem.size(),
            dtype=elem.dtype, layout=elem.layout,
            device=elem.device, requires_grad=elem.requires_grad)
        r.elem = elem
        return r

    def __repr__(self):
        return f"CompositeCompliantTensor({self.elem})"

    @classmethod
    def __torch_dispatch__(cls, func, types, args=(), kwargs=None):
        def unwrap(e):
            return e.elem if isinstance(e, CompositeCompliantTensor) else e

        def wrap(e):
            return CompositeCompliantTensor(e) if isinstance(e, torch.Tensor) else e

        if func.__name__ in ('set_', 'resize_'):
            raise RuntimeError(
                f"{func.__name__} is not allowed to be called inside of "
                f"CompositeImplicitAutograd operators.")

        with no_dispatch():
            unwrapped_args = tree_map(unwrap, args)
            unwrapped_kwargs = tree_map(unwrap, kwargs)
            unwrapped_rs = func(*unwrapped_args, **unwrapped_kwargs)
            rs = tree_map(wrap, unwrapped_rs)

        if is_view_fn(func):
            # Autograd asserts that for B = A.view_fn(...), B and A's storages
            # are the same. Here we try to make B alias A to avoid those asserts.
            aliased_tensor = args[0]
            with no_dispatch():
                x = torch.empty(aliased_tensor.shape, dtype=aliased_tensor.dtype,
                                device=aliased_tensor.device)
                x.set_(aliased_tensor)
                args_with_x = list(args)
                args_with_x[0] = x
                result = func(*args_with_x, **kwargs)
                if isinstance(result, tuple) or isinstance(result, list):
                    for a, b in zip(rs, result):
                        a.set_(b)
                else:
                    rs.set_(result)

        # Some operations are allowed to in-place modify the metadata of the
        # inputs. The only ones are the "inplace view functions"; when we
        # run into these, we manually modify the metadata of the input.
        with no_dispatch():
            if is_inplace_view_fn(func):
                func(args[0])

        # For each CompositeCompliantTensor t, we check that t and t.elem
        # have consistent metadata. If they don't have consistent metadata,
        # that means the operator did something fishy.
        check = partial(check_metadata_consistency)
        tree_map(check, args)
        tree_map(check, kwargs)
        tree_map(check, rs)
        return rs

# The general strategy is to wrap all Tensor args and kwargs in
# CompositeCompliantTensor wrappers. If an operator that is
# CompositeImplicitAutograd does any non-compliant behavior,
# CompositeCompliantTensor will raise an error.
def _check_composite_compliance(op, args, kwargs):
    def wrap(e):
        return CompositeCompliantTensor(e) if isinstance(e, torch.Tensor) else e

    args = tree_map(wrap, args)
    kwargs = tree_map(wrap, kwargs)
    try:
        with enable_python_mode(CompositeCompliantTensor):
            op(*args, **kwargs)
    except RuntimeError as err:
        raise RuntimeError(f"CompositeImplicitAutograd compilance check failed with "
                           f"the following error. If you are adding an OpInfo of an "
                           f"existing operator, please feel free to skip this test "
                           f"because the problem was pre-existing and file an issue. "
                           f"Otherwise, if you added a new operator, please read "
                           f"through the CompositeImplicitAutograd Compliance section in "
                           f"aten/src/ATen/native/README.md for how to resolve this. "
                           f"Got error message: {err.args[0]}")
