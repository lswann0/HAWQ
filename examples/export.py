"""
Exporting HAWQ models to the ONNX format.

TODO:
    Confirm attributes of QuantAct
    Organization  
    High Level API Call (HAWQ -> ONNX)
    High Level API Call (PyTorch -> HAWQ)
"""

import warnings
import os, sys
sys.path.insert(1, os.path.abspath('.'))

import torch
from torch.onnx import register_custom_op_symbolic
from torch._C import ListType, OptionalType

from utils import q_jettagger_model, q_resnet18, q_mnist
from utils.export.utils import set_export_mode
from utils.quantization_utils.quant_modules import QuantAct, QuantLinear, QuantBnConv2d
from utils.quantization_utils.quant_modules import QuantMaxPool2d, QuantAveragePool2d, QuantConv2d


SUPPORTED_LAYERS = (QuantAct, 
                    QuantLinear,
                    QuantConv2d)

UNSUPPORTED_LAYERS = (QuantBnConv2d, 
                      QuantMaxPool2d, 
                      QuantAveragePool2d)

# https://github.com/Xilinx/finn-base/tree/dev/src/finn/custom_op/general
SUPPORTED_QONNX = ['Quant', 'BipolarQuant']
UNSUPPORTED_QONNX = ['Trunc']

DOMAIN_STRING = 'hawq_onnx'

# ------------------------------------------------------------
class QuantFunc(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, scale, zero_point, bit_width, signed, narrow_range, rounding_mode):
        return x

    @staticmethod
    def symbolic(g, x, scale, zero_point, bit_width, signed, narrow_range, rounding_mode):
        ret = g.op(f'{DOMAIN_STRING}::Quant', 
                x, scale, zero_point, bit_width,  
                rounding_mode_s=rounding_mode,
                signed_i=int(signed),
                narrow_i=int(narrow_range))
        ret.setType(OptionalType.ofTensor())
        return ret


class BinaryQuantFunc(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, scale):
        return x

    @staticmethod
    def symbolic(g, x, scale):
        return g.op(f'{DOMAIN_STRING}::BipolarQuant', x, scale)


class TruncFunc(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, scale, zero_point, input_bit_width, output_bit_width, rounding_mode):
        return x

    @staticmethod
    def symbolic(g, x, scale, zero_point, input_bit_width, output_bit_width, rounding_mode):
        return g.op(f'{DOMAIN_STRING}::Trunc',
                    x, scale, zero_point, input_bit_width, output_bit_width,
                    rounding_mode_s=rounding_mode)

# ------------------------------------------------------------
class ExportQuantAct(torch.nn.Module):
    def __init__(self, layer) -> None:
        super().__init__()
        self.export_mode = True

        self.scale = layer.act_scaling_factor.clone().detach().requires_grad_(False)
        self.zero_point = torch.tensor(0)
        if layer.full_precision_flag:
            self.bit_width = torch.tensor(32)
        else:
            self.bit_width = torch.tensor(layer.activation_bit)
        
        self.narrow_range = 0
        self.rounding_mode = 'ROUND'
        if layer.quant_mode == 'symmetric':
            self.signed = 1 
        else:
            self.signed = 0    
    
    def forward(self, x, act_scaling_factor=None, weight_scaling_factor=None):
        if self.bit_width == 1:
            return BinaryQuantFunc.apply(x, self.scale), act_scaling_factor 
        return QuantFunc.apply(x, self.scale, self.zero_point, self.bit_width,
                    self.signed, self.narrow_range, self.rounding_mode), act_scaling_factor


# ------------------------------------------------------------
class ExportQuantLinear(torch.nn.Module):
    def __init__(self, layer) -> None:
        super().__init__()
        in_features, out_features = layer.weight.shape[1], layer.weight.shape[0]
        
        has_bias = hasattr(layer, 'bias')
        self.fc = torch.nn.Linear(in_features, out_features, has_bias)
        
        if has_bias:
            if layer.fix_flag:
                self.fc.weight.data = layer.weight
                self.fc.bias.data = layer.bias
            else:
                self.fc.weight.data = layer.weight_integer
                self.fc.bias.data = layer.bias_integer
        else:
            if layer.fix_flag:
                self.fc.weight.data = layer.weight
            else:
                self.fc.weight.data = layer.weight_integer

    def forward(self, x, act_scaling_factor=None, weight_scaling_factor=None):
        return self.fc(x), act_scaling_factor


# ------------------------------------------------------------
class ExportQuantConv2d(torch.nn.Module):
    def __init__(self, layer) -> None:
        super().__init__()
        self.conv = layer.conv

    def forward(self, x, act_scaling_factor=None):
        return self.conv(x), act_scaling_factor

# ------------------------------------------------------------
def replace_layers(model):
    for param in model.parameters():
        param.requires_grad_(False)

    for name in dir(model):
        layer = getattr(model, name)
        if isinstance(layer, QuantAct):
            setattr(model, name, ExportQuantAct(layer))
        elif isinstance(layer, QuantLinear):
            setattr(model, name, ExportQuantLinear(layer))
        elif isinstance(layer, QuantConv2d):
            setattr(model, name, ExportQuantConv2d(layer))
        elif isinstance(layer, torch.nn.Sequential):
            replace_layers(layer)
        elif isinstance(layer, UNSUPPORTED_LAYERS):
            raise RuntimeError(f'Unsupported layer type found {layer._get_name()}')


# ------------------------------------------------------------
def register_custom_ops():
    register_custom_op_symbolic(f'{DOMAIN_STRING}::Quant', QuantFunc.symbolic, 1)
    register_custom_op_symbolic(f'{DOMAIN_STRING}::BipolarQuant', BinaryQuantFunc.symbolic, 1)

def export_to_qonnx(model, input, filename=None):
    if model is None:
        raise RuntimeError('Model cannot be None')
    if input is None:
        raise RuntimeError('Input cannot be None')

    if filename is None:
        from datetime import datetime
        now = datetime.now() # current date and time
        date_time = now.strftime('%m%d%Y%H%M%S')
        filename = f'{model._get_name()}_{date_time}.onnx'

    register_custom_ops()
    import copy 
    export_model = copy.deepcopy(model)

    with torch.no_grad():
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            replace_layers(export_model)
            torch.onnx.export(export_model, input, filename)
        
    return export_model

# ------------------------------------------------------------
if __name__ == '__main__':
    x = torch.randn([1, 1, 28, 28])
    model = q_mnist()

    export_model = export_to_qonnx(model, x)

    # import torchvision.models as models
    # resnet18 = models.resnet18()
    # print(replace_all(q_resnet18(resnet18))
