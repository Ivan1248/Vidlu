import torch

# noinspection PyUnresolvedReferences
import _context
from vidlu.transforms import jitter

input_shape = (1024, 2048)
crop_shape = (768, 768)
x = torch.ones((3,) + input_shape)
y = torch.randint(1, 3, (1,) + input_shape)

for i in range(100):
    jitt = jitter.SegRandScaleCropPadHFlip(shape=crop_shape, max_scale=2, overflow='half')
    x_, y_ = jitt((x, y))
    if torch.any(y_ == 0):
        print(i, y_)
