import os
import shutil
import multiprocessing
from tqdm import tqdm
import pickle
from collections import Sequence
import inspect

import numpy as np

from vidlu.data import Dataset
from vidlu import data
from vidlu.data.datasets import DatasetFactory
from vidlu.utils.image.data_augmentation import random_fliplr_with_label, augment_cifar
from . import dirs

# Datasets #########################################################################################

get_dataset_set = DatasetFactory(dirs.DATASETS)


# Normalization ####################################################################################

def get_input_mean_std(dataset):
    ms = np.array([(x.mean((0, 1)), x.std((0, 1))) for x, y in dataset])
    m, s = ms.mean(0)
    return m, s


class LazyNormalizer:
    def __init__(self, ds, cache_dir=None, max_sample_size=10000):
        self.ds = ds
        if len(self.ds) > max_sample_size:
            self.ds = self.ds.permute().subset(np.arange(max_sample_size))

        self.initialized = multiprocessing.Value('i', 0)
        self.mean, self.std = (multiprocessing.Array('f', x) for x in
                               get_input_mean_std([ds[0], ds[1]]))

        self.cache_dir = f"{cache_dir}/lazy-normalizer-cache"
        self.cache_path = f"{self.cache_dir}/{ds.name}.p"

    def _initialize(self):
        mean_std = None
        if os.path.exists(self.cache_path):
            try:
                print(f"Loading dataset statistics for {self.ds.name}")
                with open(self.cache_path, 'rb') as cache_file:
                    mean_std = pickle.load(cache_file)
            except:
                os.remove(self.cache_path)
        if mean_std is None:
            print(f"Computing dataset statistics for {self.ds.name}")
            mean_std = get_input_mean_std(tqdm(self.ds))
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(f"{self.cache_path}", 'wb') as cache_file:
                pickle.dump(mean_std, cache_file, protocol=4)
        self.mean[:], self.std[:] = mean_std

    def normalize(self, x):
        with self.initialized.get_lock():
            if not self.initialized.value:  # lazy
                self._initialize()
                print("mean, std = ", np.array(self.mean))
                self.initialized.value = True
        # CAUTION! np.array(self.mean) != np.array(self.mean.value)
        breakpoint()
        return ((x - self.mean) / self.std).astype(np.float32)


# Caching ##########################################################################################

def example_byte_size(example):
    # assuming img will be float32 after normalization
    img, lab = example
    return img.astype(np.float32).nbytes + np.array(lab).nbytes


# Cached dataset with normalized inputs ############################################################

def get_cached_dataset_set_with_normalized_inputs(ds_id, **options):
    print("Setting up data preprocessing...")
    dss = get_dataset_set(ds_id, **options)
    normalizer = LazyNormalizer(dss.trainval, dirs.CACHE)

    def transform(ds):
        ds = ds.map(normalizer.normalize, 0)
        if ds_id not in ['inaturalist18']:
            ds = ds.cache_hdd_only(f"{dirs.CACHE}/datasets")
        return ds

    print("Setting up data caching on HDD...")
    return dss.with_transform(transform)


def clear_dataset_hdd_cache(ds):
    if hasattr(ds, 'cache_dir'):
        shutil.rmtree(ds.cache_dir)
        print(f"Deleted {ds.cache_dir}")
    elif isinstance(ds, data.dataset.MapDataset):  # lazynormalizer
        cache_path = inspect.getclosurevars(ds._func).nonlocals['f'].__self__.cache_path
        if os.path.exists(cache_path):
            os.remove(cache_path)
            print(f"Deleted {cache_path}")
    for k in dir(ds):
        a = getattr(ds, k)
        if isinstance(a, Dataset):
            clear_dataset_hdd_cache(a)
        elif isinstance(a, Sequence):
            for b in a:
                if isinstance(b, Dataset):
                    clear_dataset_hdd_cache(b)


# Augmentation #####################################################################################

def get_default_augmentation_func(dataset):
    if any(lambda x: dataset.name.lower().startswith(x)
           for x in ['cifar', 'cifar100', 'tinyimagenet']):
        return lambda xy: (augment_cifar(xy[0]), xy[1])
    elif dataset.info['problem_id'] == 'semseg':
        return random_fliplr_with_label
    else:
        return lambda x: x
