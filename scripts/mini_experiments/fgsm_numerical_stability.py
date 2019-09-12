import torch
from torch.nn.functional import cross_entropy
from torch import autograd

from _context import vidlu, dirs
from vidlu.experiments import TrainingExperiment, get_prepared_data_for_trainer, get_model
from vidlu import models
from vidlu.modules import loss
from vidlu.utils import func
from vidlu.utils.func import ArgTree as t

from vidlu.factories import get_data

data = get_prepared_data_for_trainer("cifar10{train,val}", dirs.DATASETS, dirs.CACHE).test
model = get_model("ResNetV2,backbone_f=t(depth=10, small_input=True)", "id", data,
                  torch.device("cpu"), 2)

x, y = data[0]
x = x.view(1, *x.shape).detach().clone().requires_grad_()
y = y.view(1, *y.shape).detach().clone()

output = model(x)*100+100
loss1 = cross_entropy(output, y)
loss2 = cross_entropy(output - output.max(), y)
loss3 = cross_entropy(output - output.max(dim=1, keepdim=True)[0], y)

grad1 = autograd.grad(loss1, x, retain_graph=True)[0]
grad2 = autograd.grad(loss2, x, retain_graph=True)[0]
grad3 = autograd.grad(loss3, x, retain_graph=True)[0]

diff12 = (grad1-grad2).abs().max()
diff13 = (grad1-grad3).abs().max()
diff23 = (grad2-grad3).abs().max()

print(diff12, diff13, diff23)

from IPython import embed

embed()