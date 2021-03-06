import shutil
from argparse import Namespace
from vidlu.utils.func import partial
from pathlib import Path
import warnings

import numpy as np
from tqdm import tqdm

from vidlu.data import DatasetFactory
from vidlu.utils import path


# Standardization ##################################################################################

def compute_pixel_mean_std(dataset, scale01=False, progress_bar=False):
    pbar = tqdm if progress_bar else lambda x: x
    mvn = np.array([(x.mean((0, 1)), x.var((0, 1)), np.prod(x.shape[:2]))
                    for x in pbar(dataset.map(lambda r: np.array(r.x)))])
    means, vars_, ns = [mvn[:, i] for i in range(3)]  # means, variances, pixel counts
    ws = ns / ns.sum()  # image weights (pixels in image / pixels in all images)
    mean = ws.dot(means)  # mean pixel
    var = vars_.mean(0) + ws.dot(means ** 2) - mean ** 2  # pixel variance
    std = np.sqrt(var)  # pixel standard deviation
    return (mean / 255, std / 255) if scale01 else (mean, std)


# Image statistics cache ###########################################################################

# not local for picklability, used only in add_image_statistics_to_info_lazily
def _get_standardization(_, ds_with_info):
    return ds_with_info.info.cache['standardization']


# not local for picklability, used only in add_image_statistics_to_info_lazily
def _compute_pixel_mean_std_d(ds):
    mean, std = compute_pixel_mean_std(ds, scale01=True, progress_bar=True)
    return Namespace(mean=mean, std=std)


def add_image_statistics_to_info_lazily(parted_dataset, cache_dir):
    try:
        stats_ds = parted_dataset.trainval
    except KeyError:
        part_name, stats_ds = next(parted_dataset.items())
        warnings.warn('The parted dataset object has no "trainval" part.'
                      + f' "{part_name}" is used instead.')
    ds_with_info = stats_ds.info_cache_hdd(
        dict(standardization=_compute_pixel_mean_std_d), Path(cache_dir) / 'dataset_statistics')

    def cache_transform(ds):
        return ds.info_cache(
            dict(standardization=partial(_get_standardization, ds_with_info=ds_with_info)))

    return parted_dataset.with_transform(cache_transform)


# Caching ##########################################################################################

def cache_data_lazily(parted_dataset, cache_dir, min_free_space=20 * 2 ** 30):
    elem_size = next(parted_dataset.items())[1].approx_example_size()
    size = elem_size * sum(len(ds) for _, ds in parted_dataset.top_level_items())
    free_space = shutil.disk_usage(cache_dir).free
    space_left = free_space - size

    def transform(ds):
        ds_cached = ds.cache_hdd(f"{cache_dir}/datasets")
        has_been_cached = path.get_size(ds_cached.cache_dir) > size * 0.1
        if has_been_cached or space_left >= min_free_space:
            ds = ds_cached
        else:
            ds_cached.delete_cache()
            del ds_cached
            warnings.warn(f'The dataset {ds.identifier} will not be cached because there is not'
                          + f' much space left.'
                          + f'\nAvailable space: {free_space / 2 ** 30:.3f} GiB.'
                          + f'\nData size: {elem_size * len(ds) / 2 ** 30:.3f} GiB (subset),'
                          + f' {size / 2 ** 30:.3f} GiB (all).')
        return ds

    return parted_dataset.with_transform(transform)


class CachingDatasetFactory(DatasetFactory):
    def __init__(self, datasets_dir_or_factory, cache_dir, parted_ds_transforms=()):
        ddof = datasets_dir_or_factory
        super().__init__(ddof.datasets_dirs if isinstance(ddof, DatasetFactory) else ddof)
        self.cache_dir = cache_dir
        self.parted_ds_transforms = parted_ds_transforms

    def __call__(self, ds_name, **kwargs):
        pds = super().__call__(ds_name, **kwargs)
        for transform in self.parted_ds_transforms:
            pds = transform(pds)
        return cache_data_lazily(pds, self.cache_dir)
