import random

from .transforms import image_transformer


def cifar_jitter(x):
    pilt = image_transformer(x).to_pil()
    return pilt.rand_crop(pilt.item.size, padding=4).rand_hflip().item


def rand_hflip(*arrays, p=0.5):
    if p < random.random:
        return [image_transformer(a).hflip() for a in arrays]
    if len(arrays) == 1:
        return arrays[0]
    return arrays