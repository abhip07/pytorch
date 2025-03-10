from test_pytorch_common import TestCase, run_tests

import torch
import torch.onnx
from torch.onnx import utils, OperatorExportTypes, TrainingMode, register_custom_op_symbolic
from torch.onnx.symbolic_helper import _set_opset_version, _set_operator_export_type, _set_onnx_shape_inference
import torch.utils.cpp_extension
from test_pytorch_common import (skipIfUnsupportedMinOpsetVersion,
                                 skipIfUnsupportedMaxOpsetVersion)
import caffe2.python.onnx.backend as backend
from verify import verify

import torchvision

import onnx

import io
import copy
import unittest


skip = unittest.skip


class _BaseTestCase(TestCase):

    def setUp(self):
        torch.manual_seed(0)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(0)

    def _model_to_graph(self, model, input,
                        do_constant_folding=True,
                        training=TrainingMode.EVAL,
                        operator_export_type=OperatorExportTypes.ONNX,
                        input_names=None,
                        dynamic_axes=None):
        if training == torch.onnx.TrainingMode.TRAINING:
            model.train()
        elif training == torch.onnx.TrainingMode.EVAL:
            model.eval()
        # Need disable onnx_shape_inference for this test because it puts const node to initializers.
        _set_onnx_shape_inference(False)
        utils._validate_dynamic_axes(dynamic_axes, model, None, None)
        graph, params_dict, torch_out = utils._model_to_graph(model, input,
                                                              do_constant_folding=do_constant_folding,
                                                              _disable_torch_constant_prop=True,
                                                              operator_export_type=operator_export_type,
                                                              training=training,
                                                              input_names=input_names,
                                                              dynamic_axes=dynamic_axes)
        _set_onnx_shape_inference(True)
        return graph, params_dict, torch_out


class TestUtilityFuns_opset_independent(_BaseTestCase):

    def test_unconvertible_ops(self):
        class MyModule(torch.nn.Module):
            def forward(self, x):
                return torch.cumsum(x, dim=0)

        model = MyModule()
        x = torch.randn(2, 3, 4)

        graph, unconvertible_ops = utils.unconvertible_ops(model, (x,), opset_version=9)
        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "onnx::Constant")
        self.assertEqual(next(iter).kind(), "prim::Constant")
        self.assertEqual(next(iter).kind(), "aten::cumsum")
        self.assertEqual(len(unconvertible_ops), 1)
        self.assertEqual(unconvertible_ops, ["aten::cumsum"])


class TestUtilityFuns_opset9(_BaseTestCase):
    opset_version = 9

    def test_is_in_onnx_export(self):
        test_self = self

        class MyModule(torch.nn.Module):
            def forward(self, x):
                test_self.assertTrue(torch.onnx.is_in_onnx_export())
                raise ValueError
                return x + 1

        x = torch.randn(3, 4)
        f = io.BytesIO()
        try:
            torch.onnx.export(MyModule(), x, f, opset_version=self.opset_version)
        except ValueError:
            self.assertFalse(torch.onnx.is_in_onnx_export())

    def test_validate_dynamic_axes_invalid_input_output_name(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            utils._validate_dynamic_axes({"input1": {}, "output": {},
                                         "invalid_name1": {}, "invalid_name2": {}},
                                         None, ["input1", "input2"], ["output"])
            messages = [str(warning.message) for warning in w]
        self.assertIn(
            "Provided key invalid_name1 for dynamic axes is not a valid input/output name",
            messages)
        self.assertIn(
            "Provided key invalid_name2 for dynamic axes is not a valid input/output name",
            messages)
        self.assertEqual(len(messages), 2)

    @skipIfUnsupportedMinOpsetVersion(11)
    def test_split_to_slice(self):
        class SplitModule(torch.nn.Module):
            def forward(self, x, y, t):
                splits = (x.size(1), y.size(1))
                out, out2 = torch.split(t, splits, dim=1)
                return out, out2

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.randn(2, 3)
        y = torch.randn(2, 4)
        t = torch.randn(2, 7)
        graph, _, _ = self._model_to_graph(SplitModule(), (x, y, t), input_names=["x", "y", "t"],
                                           dynamic_axes={"x": [0, 1], "y": [0, 1], "t": [0, 1]})
        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::SplitToSequence")

    def test_output_list(self):
        class PaddingLayer(torch.jit.ScriptModule):
            @torch.jit.script_method
            def forward(self, input_t, n):
                # type: (Tensor, int) -> Tensor
                for i in range(n):
                    input_t = input_t * 2
                return input_t

        input_t = torch.ones(size=[10], dtype=torch.long)
        n = 2
        model = torch.jit.script(PaddingLayer())
        example_output = model(input_t, n)

        with self.assertRaises(RuntimeError):
            torch.onnx._export(model,
                               (input_t, n),
                               "test.onnx",
                               opset_version=self.opset_version,
                               example_outputs=[example_output])

    def test_constant_fold_transpose(self):
        class TransposeModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = torch.transpose(a, 1, 0)
                return b + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(3, 2)
        graph, _, __ = self._model_to_graph(TransposeModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Transpose")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_reduceL2(self):
        class ReduceModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = torch.norm(a, p=2, dim=-2, keepdim=False)
                return b + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(2, 3)
        graph, _, __ = self._model_to_graph(ReduceModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::ReduceL2")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_reduceL1(self):
        class NormModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = torch.norm(a, p=1, dim=-2)
                return b + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(2, 3)
        graph, _, __ = self._model_to_graph(NormModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::ReduceL1")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_slice(self):
        class NarrowModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = torch.narrow(a, 0, 0, 1)
                return b + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(1, 3)
        graph, _, __ = self._model_to_graph(NarrowModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Slice")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_slice_index_exceeds_dim(self):
        class SliceIndexExceedsDimModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = a[1:10]         # index exceeds dimension
                return b + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(1, 3)
        graph, _, __ = self._model_to_graph(SliceIndexExceedsDimModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Slice")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_slice_negative_index(self):
        class SliceNegativeIndexModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = a[0:-1]        # index relative to the end
                c = torch.select(a, dim=-1, index=-2)
                d = torch.select(a, dim=1, index=0)
                return b + x, c + d

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(1, 3)
        graph, _, __ = self._model_to_graph(SliceNegativeIndexModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Slice")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")

    def test_constant_fold_gather(self):
        class GatherModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = torch.select(a, dim=1, index=-2)
                c = torch.index_select(a, dim=-2, index=torch.tensor([0, 1]))
                return b + 1, c + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(1, 3)
        model = GatherModule()
        model(x)
        graph, _, __ = self._model_to_graph(GatherModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Gather")

    def test_constant_fold_unsqueeze(self):
        class UnsqueezeModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[1., 2., 3.], [4., 5., 6.]])
                b = torch.unsqueeze(a, -2)
                return b + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(1, 2, 3)
        graph, _, __ = self._model_to_graph(UnsqueezeModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1, 2]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Unsqueeze")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_unsqueeze_multi_axies(self):
        class PReluModel(torch.nn.Module):
            def __init__(self):
                super(PReluModel, self).__init__()
                self.prelu = torch.nn.PReLU()

            def forward(self, x):
                a = torch.randn(2, 3, 4, 5, 8, 7)
                return self.prelu(x) + a

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.randn(2, 3, 4, 5, 8, 7)
        graph, _, __ = self._model_to_graph(PReluModel(), x, input_names=["x"],
                                            dynamic_axes={"x": [0, 1, 2, 3, 4, 5]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Unsqueeze")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 4)

    def test_constant_fold_squeeze_without_axes(self):
        class SqueezeModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[[1., 2., 3.], [4., 5., 6.]]])
                return torch.squeeze(a) + x + torch.squeeze(a)

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(2, 3)
        graph, _, __ = self._model_to_graph(SqueezeModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})
        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Squeeze")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 2)

    def test_constant_fold_squeeze_with_axes(self):
        class SqueezeAxesModule(torch.nn.Module):
            def forward(self, x):
                a = torch.tensor([[[1., 2., 3.], [4., 5., 6.]]])
                return torch.squeeze(a, dim=-3) + x

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(2, 3)
        graph, _, __ = self._model_to_graph(SqueezeAxesModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Squeeze")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_concat(self):
        class ConcatModule(torch.nn.Module):
            def forward(self, x):
                # Why did I insert a Cast here?  There appears to be intentional
                # behavior in ONNX constant folding where constant tensors which
                # are not attached to any known to be foldable onnx
                # operations don't get extracted into the initializer graph.  So
                # without these casts, we will actually fail to pull out one of
                # the constants, thus failing constant folding.  I think the
                # test is wrong but I don't have time to write a more correct
                # test (I think the right way to go about the test is to setup
                # a predicate for what invariant graphs should hold after
                # constant folding, and then verify this predicate holds.
                # I think the asserts below are an attempt at this predicate,
                # but it is not right!)
                #
                # More commentary at
                # https://github.com/pytorch/pytorch/pull/18698/files#r340107552
                a = torch.tensor([[1., 2., 3.]]).to(torch.float)
                b = torch.tensor([[4., 5., 6.]]).to(torch.float)
                c = torch.cat((a, b), 0)
                d = b + c
                return x + d

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.ones(2, 3)
        graph, _, __ = self._model_to_graph(ConcatModule(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Concat")
            self.assertNotEqual(node.kind(), "onnx::Cast")
            self.assertNotEqual(node.kind(), "onnx::Constant")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_lstm(self):
        class GruNet(torch.nn.Module):
            def __init__(self):
                super(GruNet, self).__init__()
                self.mygru = torch.nn.GRU(7, 3, 1, bidirectional=False)

            def forward(self, input, initial_state):
                return self.mygru(input, initial_state)

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        input = torch.randn(5, 3, 7)
        h0 = torch.randn(1, 3, 3)
        graph, _, __ = self._model_to_graph(GruNet(), (input, h0), input_names=["input", "h0"],
                                            dynamic_axes={"input": [0, 1, 2], "h0": [0, 1, 2]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Slice")
            self.assertNotEqual(node.kind(), "onnx::Concat")
            self.assertNotEqual(node.kind(), "onnx::Unsqueeze")

        if self.opset_version <= 12:
            self.assertEqual(len(list(graph.nodes())), 3)
        else:
            # Unsqueeze op parameter "axes" as an input instead of as an attribute when opset version >= 13
            self.assertEqual(len(list(graph.nodes())), 4)

    def test_constant_fold_transpose_matmul(self):
        class MatMulNet(torch.nn.Module):
            def __init__(self):
                super(MatMulNet, self).__init__()
                self.B = torch.nn.Parameter(torch.ones(5, 3))

            def forward(self, A):
                return torch.matmul(A, torch.transpose(self.B, -1, -2))

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        A = torch.randn(2, 3)
        graph, _, __ = self._model_to_graph(MatMulNet(), (A, ),
                                            input_names=["A"], dynamic_axes={"A": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Transpose")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_reshape(self):
        class ReshapeModule(torch.nn.Module):
            def __init__(self, ):
                super(ReshapeModule, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                b = self.weight.reshape(1, -1, 1, 1)
                return x * b

        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        x = torch.randn(4, 5)
        graph, _, __ = self._model_to_graph(ReshapeModule(), (x, ),
                                            input_names=["x"], dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Reshape")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_div(self):
        class Module(torch.nn.Module):
            def __init__(self, ):
                super(Module, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                div = self.weight.div(torch.tensor([1, 2, 3, 4, 5]))
                return div * x

        x = torch.randn(2, 5)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, _, __ = self._model_to_graph(Module(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Div")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_mul(self):
        class Module(torch.nn.Module):
            def __init__(self, ):
                super(Module, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                mul = self.weight.mul(torch.tensor([1, 2, 3, 4, 5]))
                return mul / x

        x = torch.randn(2, 5)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, _, __ = self._model_to_graph(Module(), (x, ), input_names=["x"],
                                            dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Mul")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_add(self):
        class Module(torch.nn.Module):
            def __init__(self, ):
                super(Module, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                add = self.weight + torch.tensor([1, 2, 3, 4, 5])
                return add - x

        x = torch.randn(2, 5)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, params_dict, __ = self._model_to_graph(
            Module(), (x, ), do_constant_folding=True,
            operator_export_type=OperatorExportTypes.ONNX,
            input_names=["x"], dynamic_axes={"x": [0, 1]})
        for node in graph.nodes():
            self.assertTrue(node.kind() != "onnx::Add")
        self.assertEqual(len(list(graph.nodes())), 1)
        params = list(params_dict.values())
        self.assertEqual(len(params), 1)
        weight = params[0]
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(weight, torch.tensor([2, 3, 4, 5, 6]))

    def test_constant_fold_sub(self):
        class Module(torch.nn.Module):
            def __init__(self, ):
                super(Module, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                sub = self.weight - torch.tensor([1, 2, 3, 4, 5])
                return sub + x

        x = torch.randn(2, 5)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, params_dict, __ = self._model_to_graph(
            Module(), (x, ), do_constant_folding=True,
            operator_export_type=OperatorExportTypes.ONNX, input_names=["x"], dynamic_axes={"x": [0, 1]})
        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Sub")
        self.assertEqual(len(list(graph.nodes())), 1)
        params = list(params_dict.values())
        self.assertEqual(len(params), 1)
        weight = params[0]
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(weight, torch.tensor([0, -1, -2, -3, -4]))

    def test_constant_fold_sqrt(self):
        class Module(torch.nn.Module):
            def __init__(self, ):
                super(Module, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                sqrt = torch.sqrt(self.weight)
                return sqrt / x

        x = torch.randn(2, 5)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, _, __ = self._model_to_graph(Module(), (x, ), input_names=["x"], dynamic_axes={"x": [0, 1]})
        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Sqrt")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_constant_fold_shape(self):
        class ShapeModule(torch.nn.Module):
            def __init__(self):
                super(ShapeModule, self).__init__()
                self.register_buffer("weight", torch.ones(5))

            def forward(self, x):
                shape = self.weight.shape[0]
                return x + shape

        x = torch.randn(2, 5)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, _, __ = self._model_to_graph(ShapeModule(), (x, ), input_names=["x"], dynamic_axes={"x": [0, 1]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::Shape")
        self.assertEqual(len(list(graph.nodes())), 1)

    def test_verbose(self):
        class MyModule(torch.nn.Module):
            def forward(self, input):
                return torch.exp(input)
        x = torch.randn(3, 4)

        def is_model_stripped(f, verbose=None):
            if verbose is None:
                torch.onnx.export(MyModule(), x, f, opset_version=self.opset_version)
            else:
                torch.onnx.export(MyModule(), x, f, verbose=verbose,
                                  opset_version=self.opset_version)
            model = onnx.load(io.BytesIO(f.getvalue()))
            model_strip = copy.copy(model)
            onnx.helper.strip_doc_string(model_strip)
            return model == model_strip

        # test verbose=False (default)
        self.assertTrue(is_model_stripped(io.BytesIO()))
        # test verbose=True
        self.assertFalse(is_model_stripped(io.BytesIO(), True))

    # NB: remove this test once DataParallel can be correctly handled
    def test_error_on_data_parallel(self):
        model = torch.nn.DataParallel(torch.nn.ReflectionPad2d((1, 2, 3, 4)))
        x = torch.randn(1, 2, 3, 4)
        f = io.BytesIO()
        with self.assertRaisesRegex(ValueError,
                                    "torch.nn.DataParallel is not supported by ONNX "
                                    "exporter, please use 'attribute' module to "
                                    "unwrap model from torch.nn.DataParallel. Try "):
            torch.onnx.export(model, x, f, opset_version=self.opset_version)

    def test_export_mode(self):
        class MyModule(torch.nn.Module):
            def forward(self, x):
                y = x + 1
                return y

        model = MyModule()
        x = torch.randn(10, 3, 128, 128)
        f = io.BytesIO()

        # set mode to in inference mode and export in training mode
        model.eval()
        old_state = model.training
        torch.onnx.export(model, (x,), f,
                          opset_version=self.opset_version, training=torch.onnx.TrainingMode.TRAINING)
        # verify that the model state is preserved
        self.assertEqual(model.training, old_state)

        # set mode to training mode and export in inference mode
        model.train()
        old_state = model.training
        torch.onnx.export(model, (x,), f,
                          opset_version=self.opset_version, training=torch.onnx.TrainingMode.EVAL)
        # verify that the model state is preserved
        self.assertEqual(model.training, old_state)

    @skipIfUnsupportedMinOpsetVersion(12)
    def test_local_function(self):
        class N(torch.nn.Module):
            def __init__(self, prob):
                super().__init__()
                self.dropout = torch.nn.Dropout(prob)

            def forward(self, x):
                return self.dropout(x)

        class M(torch.nn.Module):
            def __init__(self, num_layers):
                super().__init__()
                self.num_layers = num_layers
                self.lns = torch.nn.ModuleList([torch.nn.LayerNorm(3, eps=i) for i in range(num_layers)])
                self.celu1 = torch.nn.CELU(1.0)
                self.celu2 = torch.nn.CELU(2.0)
                self.dropout = N(0.5)

            def forward(self, x: torch.Tensor, y: torch.Tensor, z: torch.Tensor) -> torch.Tensor:
                res1 = self.celu1(x)
                res2 = self.celu2(y)
                for ln in self.lns:
                    z = ln(z)
                return res1 + res2, self.dropout(z)

        x = torch.randn(2, 3)
        y = torch.randn(2, 3)
        z = torch.randn(2, 3)

        """
        Export specified modules, containing non-existed dropout.
        """
        f = io.BytesIO()
        torch.onnx.export(M(3), (x, y, z), f, opset_version=self.opset_version,
                          export_modules_as_functions={torch.nn.CELU, torch.nn.Dropout, torch.nn.LayerNorm})

        onnx_model = onnx.load(io.BytesIO(f.getvalue()))

        # Check function definition
        funcs = onnx_model.functions
        celu_funcs = [f for f in funcs if f.name == "CELU"]
        self.assertEqual(len(celu_funcs), 1)
        self.assertEqual(celu_funcs[0].domain, "torch.nn.modules.activation")
        self.assertEqual(len(celu_funcs[0].attribute), 1)
        ln_funcs = [f for f in funcs if f.name == "LayerNorm"]
        self.assertEqual(len(ln_funcs), 1)
        self.assertEqual(ln_funcs[0].domain, "torch.nn.modules.normalization")
        self.assertEqual(len(ln_funcs[0].attribute), 1)

        # Check local function nodes
        nodes = onnx_model.graph.node
        celu_ns = [n for n in nodes if n.op_type == "CELU"]
        ln_ns = [n for n in nodes if n.op_type == "LayerNorm"]
        self.assertEqual(len(celu_ns), 2)
        self.assertEqual(celu_ns[0].domain, "torch.nn.modules.activation")
        self.assertEqual(len(celu_ns[0].attribute), 1)
        self.assertEqual(len(ln_ns), 3)
        self.assertEqual(ln_ns[0].domain, "torch.nn.modules.normalization")
        self.assertEqual(len(ln_ns[0].attribute), 1)

        """
        Export specified modules.
        """
        f = io.BytesIO()
        torch.onnx.export(M(3), (x, y, z), f, opset_version=self.opset_version,
                          export_modules_as_functions={"torch.nn.modules.activation.CELU"})

        onnx_model = onnx.load(io.BytesIO(f.getvalue()))
        funcs = onnx_model.functions
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0].name, "CELU")

        """
        Export with empty specified modules. Normal export.
        """
        f = io.BytesIO()
        torch.onnx.export(M(3), (x, y, z), f, opset_version=self.opset_version,
                          export_modules_as_functions=set())

        onnx_model = onnx.load(io.BytesIO(f.getvalue()))
        funcs = onnx_model.functions
        self.assertEqual(len(funcs), 0)

        """
        Export all modules. Should contain {M, CELU, LayerNorm}.
        """
        f = io.BytesIO()
        torch.onnx.export(M(3), (x, y, z), f, opset_version=self.opset_version,
                          export_modules_as_functions=True)

        onnx_model = onnx.load(io.BytesIO(f.getvalue()))
        funcs = onnx_model.functions
        self.assertEqual(len(funcs), 3)

    def test_aten_fallthrough(self):
        # Test aten export of op with no symbolic
        class Module(torch.nn.Module):
            def forward(self, x):
                return torch.erfc(x)

        x = torch.randn(2, 3, 4)
        _set_opset_version(self.opset_version)
        graph, _, __ = self._model_to_graph(Module(), (x, ),
                                            operator_export_type=OperatorExportTypes.ONNX_FALLTHROUGH,
                                            input_names=["x"], dynamic_axes={"x": [0, 1, 2]})
        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "aten::erfc")

    def test_custom_op_fallthrough(self):
        # Test custom op
        op_source = """
        #include <torch/script.h>

        torch::Tensor custom_add(torch::Tensor self, torch::Tensor other) {
          return self + other;
        }

        static auto registry =
          torch::RegisterOperators("custom_namespace::custom_op", &custom_add);
        """

        torch.utils.cpp_extension.load_inline(
            name="custom_add",
            cpp_sources=op_source,
            is_python_module=False,
            verbose=True,
        )

        class FooModel(torch.nn.Module):
            def forward(self, input, other):
                # Calling custom op
                return torch.ops.custom_namespace.custom_op(input, other)

        x = torch.randn(2, 3, 4, requires_grad=False)
        y = torch.randn(2, 3, 4, requires_grad=False)
        model = FooModel()
        graph, _, __ = self._model_to_graph(model, (x, y),
                                            operator_export_type=torch.onnx.OperatorExportTypes.ONNX_FALLTHROUGH,
                                            input_names=["x", "y"],
                                            dynamic_axes={"x": [0, 1, 2], "y": [0, 1, 2]})
        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "custom_namespace::custom_op")

    def test_custom_opsets_gelu(self):
        def gelu(g, self):
            return g.op("com.microsoft::Gelu", self).setType(self.type())

        register_custom_op_symbolic("::gelu", gelu, 1)
        model = torch.nn.GELU()
        x = torch.randn(3, 3)
        f = io.BytesIO()
        torch.onnx.export(model, (x, ), f,
                          opset_version=self.opset_version, custom_opsets={"com.microsoft": 1})

        graph = onnx.load(io.BytesIO(f.getvalue()))
        self.assertEqual(graph.graph.node[0].op_type, "Gelu")
        self.assertEqual(graph.opset_import[0].version, self.opset_version)
        self.assertEqual(graph.opset_import[1].domain, "com.microsoft")
        self.assertEqual(graph.opset_import[1].version, 1)

    def test_custom_opsets_inverse(self):
        class CustomInverse(torch.nn.Module):
            def forward(self, x):
                return torch.inverse(x) + x

        def inverse(g, self):
            return g.op("com.microsoft::Inverse", self).setType(self.type())

        register_custom_op_symbolic("::inverse", inverse, 1)
        model = CustomInverse()
        x = torch.randn(2, 3, 3)
        f = io.BytesIO()
        torch.onnx.export(model, (x, ), f,
                          opset_version=self.opset_version, custom_opsets={"com.microsoft": 1})

        graph = onnx.load(io.BytesIO(f.getvalue()))
        self.assertEqual(graph.graph.node[0].op_type, "Inverse")
        self.assertEqual(graph.opset_import[0].version, self.opset_version)
        self.assertEqual(graph.opset_import[1].domain, "com.microsoft")
        self.assertEqual(graph.opset_import[1].version, 1)

    def test_onnx_fallthrough(self):
        # Test aten export of op with symbolic for aten
        x = torch.randn(100, 128)
        y = torch.randn(100, 128)
        model = torch.nn.CosineSimilarity(dim=1, eps=1e-6)

        graph, _, __ = self._model_to_graph(model, (x, y),
                                            operator_export_type=OperatorExportTypes.ONNX_FALLTHROUGH,
                                            input_names=["x", "y"],
                                            dynamic_axes={"x": [0, 1], "y": [0, 1]})
        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "onnx::Constant")
        self.assertEqual(next(iter).kind(), "onnx::Constant")
        self.assertEqual(next(iter).kind(), "aten::cosine_similarity")

    def test_quantized_fallthrough(self):
        # Test Quantized op
        class QModule(torch.nn.Module):
            def __init__(self):
                super(QModule, self).__init__()
                self.quant1 = torch.ao.quantization.QuantStub()
                self.dequant = torch.ao.quantization.DeQuantStub()

            def forward(self, x):
                res = self.quant1(x)
                return self.dequant(res)

        model = QModule()
        torch.backends.quantized.engine = "qnnpack"
        pt_inputs = (torch.randn(1, 2, 3, 4))
        model.qconfig = torch.ao.quantization.default_qconfig
        q_model = torch.ao.quantization.prepare(model, inplace=False)
        q_model = torch.ao.quantization.convert(q_model, inplace=False)

        q_model.eval()

        graph, _, __ = self._model_to_graph(q_model, pt_inputs,
                                            operator_export_type=OperatorExportTypes.ONNX_FALLTHROUGH,
                                            input_names=["pt_inputs"],
                                            dynamic_axes={"pt_inputs": [0, 1, 2, 3]})

        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "onnx::Constant")
        self.assertEqual(next(iter).kind(), "onnx::Constant")
        self.assertEqual(next(iter).kind(), "onnx::Constant")
        self.assertEqual(next(iter).kind(), "aten::quantize_per_tensor")
        self.assertEqual(next(iter).kind(), "aten::dequantize")

    # prim::ListConstruct is exported as onnx::SequenceConstruct for opset >= 11
    @skipIfUnsupportedMaxOpsetVersion(10)
    def test_prim_fallthrough(self):
        # Test prim op
        class PrimModule(torch.jit.ScriptModule):
            @torch.jit.script_method
            def forward(self, x):
                if isinstance(x, list):
                    y = x
                else:
                    y = [x]
                return y

        x = torch.tensor([2])
        model = PrimModule()
        model.eval()
        graph, _, __ = self._model_to_graph(model, (x,),
                                            operator_export_type=OperatorExportTypes.ONNX_FALLTHROUGH,
                                            input_names=["x"], dynamic_axes={"x": [0]})
        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "prim::ListConstruct")

    def test_custom_layer_tuple(self):
        class CustomFunction(torch.autograd.Function):
            @staticmethod
            def symbolic(g, input):
                return g.op("CustomNamespace::Custom", input, outputs=2)

            @staticmethod
            def forward(ctx, input):
                return input, input

        class Custom(torch.nn.Module):
            def forward(self, input):
                return CustomFunction.apply(input)

        model = Custom()
        batch = torch.FloatTensor(1, 3)

        graph, _, _ = self._model_to_graph(model, batch,
                                           input_names=["batch"], dynamic_axes={"batch": [0, 1]})
        iter = graph.nodes()
        self.assertEqual(next(iter).kind(), "CustomNamespace::Custom")

    def test_unused_initializers(self):
        class Model(torch.nn.Module):
            def __init__(self):
                super(Model, self).__init__()
                self.conv2 = torch.nn.ConvTranspose2d(16, 33, (3, 5), stride=(2, 1), padding=(4, 2), dilation=(1, 1))
                self.k_proj = torch.nn.Linear(5, 5, bias=True)

            def forward(self, x):
                x = self.conv2(x)
                return x

        x = torch.randn(20, 16, 50, 100)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        _, params_dict, __ = self._model_to_graph(Model(), (x, ), do_constant_folding=False,
                                                  operator_export_type=OperatorExportTypes.ONNX,
                                                  input_names=["x"],
                                                  dynamic_axes={"x": [0, 1, 2, 3]})

        self.assertEqual(len(params_dict), 2)

    def test_scripting_param(self):
        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()
                self.conv = torch.nn.Conv2d(3, 16, kernel_size=1, stride=2, padding=3, bias=True)
                self.bn = torch.nn.BatchNorm2d(16, affine=True)

            def forward(self, x):
                x = self.conv(x)
                bn = self.bn(x)
                return bn

        model = torch.jit.script(MyModule())
        x = torch.randn(10, 3, 128, 128)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, _, __ = self._model_to_graph(model, (x,), do_constant_folding=True,
                                            operator_export_type=OperatorExportTypes.ONNX,
                                            training=torch.onnx.TrainingMode.TRAINING,
                                            input_names=["x"], dynamic_axes={"x": [0, 1, 2, 3]})

        graph_input_params = [param.debugName() for param in graph.inputs()]
        for item in dict(model.named_parameters()):
            self.assertIn(
                item, graph_input_params,
                "Graph parameter names does not match model parameters.")

    def test_modifying_params(self):
        class MyModel(torch.nn.Module):
            def __init__(self):
                super(MyModel, self).__init__()
                self.param = torch.nn.Parameter(torch.tensor([2.0]))

            def forward(self, x):
                y = x * x
                self.param.data.add_(1.0)
                return y

        x = torch.tensor([1, 2])
        verify(MyModel(), x, backend, do_constant_folding=False)

    def test_fuse_conv_bn(self):
        class Fuse(torch.nn.Module):
            def __init__(self):
                super(Fuse, self).__init__()
                self.conv = torch.nn.Conv2d(3, 2, kernel_size=1, stride=2, padding=3, bias=True)
                self.bn = torch.nn.BatchNorm2d(2)

            def forward(self, x):
                out = self.conv(x)
                return self.bn(out)

        x = torch.randn(2, 3, 2, 2, requires_grad=True)
        graph, _, __ = self._model_to_graph(Fuse(), (x, ),
                                            training=TrainingMode.EVAL, input_names=["x"],
                                            dynamic_axes={"x": [0, 1, 2, 3]})
        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::BatchNormalization")
            self.assertEqual(node.kind(), "onnx::Conv")

        self.assertEqual(len(list(graph.nodes())), 1)

    def test_fuse_resnet18(self):
        model = torchvision.models.resnet18(pretrained=False)
        x = torch.randn(2, 3, 224, 224, requires_grad=True)
        graph, _, __ = self._model_to_graph(model, (x, ),
                                            training=TrainingMode.EVAL,
                                            input_names=["x"], dynamic_axes={"x": [0, 1, 2, 3]})

        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "onnx::BatchNormalization")

    def test_onnx_function_substitution_pass(self):

        @torch.jit.script
        def f(x : torch.Tensor, y : torch.Tensor):
            z = x - y
            return x + z

        class MyModule(torch.nn.Module):
            def __init__(self):
                super(MyModule, self).__init__()

            def forward(self, x, y):
                return f(x, y)

        input_1 = torch.tensor(11)
        input_2 = torch.tensor(12)
        _set_opset_version(self.opset_version)
        _set_operator_export_type(OperatorExportTypes.ONNX)
        graph, _, __ = self._model_to_graph(MyModule(), (input_1, input_2), do_constant_folding=True,
                                            operator_export_type=OperatorExportTypes.ONNX,
                                            input_names=["input_1", "input_2"],
                                            dynamic_axes={"input_1": [0], "input_2": [0]})
        # Check that the prim::Constant node in the graph for representing the
        # scripted function `f` is removed and the following prim::CallFunction
        # is replced by inline graph, with onnx::Sub and onnx::Add nodes.
        for node in graph.nodes():
            self.assertNotEqual(node.kind(), "prim::Constant")
        self.assertEqual(len(list(graph.nodes())), 2)  # onnx::Sub and onnx::Add nodes only.

    def test_duplicated_output_node(self):
        class DuplicatedOutputNet(torch.nn.Module):
            def __init__(self, input_size, num_classes):
                super(DuplicatedOutputNet, self).__init__()
                self.fc1 = torch.nn.Linear(input_size, num_classes)

            def forward(self, input0, input1):
                out1 = self.fc1(input0)
                out2 = self.fc1(input1)
                return out1, out1, out2, out1, out2

        N, D_in, H, D_out = 64, 784, 500, 10
        pt_model = DuplicatedOutputNet(D_in, D_out)

        f = io.BytesIO()
        x = torch.randn(N, D_in)
        dynamic_axes = {
            "input0": {0: "input0_dim0", 1: "input0_dim1"},
            "input1": {0: "input1_dim0", 1: "input1_dim1"},
            "output-0": {0: "output-0_dim0", 1: "output-0_dim1"},
            "output-1": {0: "output-1_dim0", 1: "output-1_dim1"},
            "output-2": {0: "output-2_dim0", 1: "output-2_dim1"},
            "output-3": {0: "output-3_dim0", 1: "output-3_dim1"},
            "output-4": {0: "output-4_dim0", 1: "output-4_dim1"}}

        torch.onnx.export(pt_model,
                          (x, x),
                          f,
                          input_names=["input0", "input1"],
                          output_names=["output-0", "output-1", "output-2", "output-3", "output-4"],
                          do_constant_folding=False,
                          training=torch.onnx.TrainingMode.TRAINING,
                          dynamic_axes=dynamic_axes,
                          verbose=True,
                          keep_initializers_as_inputs=True)

        graph = onnx.load(io.BytesIO(f.getvalue()))
        self.assertEqual(graph.graph.input[0].name, "input0")
        self.assertEqual(graph.graph.input[1].name, "input1")
        for i in range(5):
            self.assertEqual(graph.graph.output[i].name, f"output-{i}")
        self.assertEqual(graph.graph.node[0].op_type, "Gemm")
        self.assertEqual(graph.graph.node[1].op_type, "Identity")
        self.assertEqual(graph.graph.node[2].op_type, "Identity")
        self.assertEqual(graph.graph.node[3].op_type, "Gemm")
        self.assertEqual(graph.graph.node[4].op_type, "Identity")


class TestUtilityFuns_opset10(TestUtilityFuns_opset9):
    opset_version = 10


class TestUtilityFuns_opset11(TestUtilityFuns_opset9):
    opset_version = 11


class TestUtilityFuns_opset12(TestUtilityFuns_opset9):
    opset_version = 12


class TestUtilityFuns_opset13(TestUtilityFuns_opset9):
    opset_version = 13


class TestUtilityFuns_opset14(TestUtilityFuns_opset9):
    opset_version = 14


if __name__ == "__main__":
    run_tests()
