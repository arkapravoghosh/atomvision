"""Simulated STEM pytorch dataloader for atom localization and crystal classification."""
import torch
from torch.utils.data import Dataset, DataLoader

import numpy as np
import pandas as pd
from skimage import draw

from jarvis.core.atoms import Atoms
from jarvis.core.specie import chem_data
from jarvis.db.figshare import data, get_jid_data

from atomvision.data.stemconv import STEMConv

from collections.abc import Callable
from typing import Optional, List, Dict, Any

from skimage import measure
from scipy.spatial import KDTree
import networkx as nx

import dgl

LABEL_MODES = {"delta", "radius"}

# atomic radii
pt = pd.DataFrame(chem_data).T
pt = pt.sort_values(by="Z")
RADII = {int(row.Z): row.atom_rad for id, row in pt.iterrows()}


def atomic_radius_mask(shape, X, N, px_scale=0.1):
    """Atom localization masks, with footprints scaled to atomic radii.

    Atoms occluding each other along the Z (transmission) dimension are
    not guaranteed to be masked nicely; these are not multilabel masks
    """
    labels = np.zeros(shape, dtype=int)
    for x, n in zip(X, N):

        rr, cc = draw.disk(tuple(x), 0.5 * RADII[n] / px_scale, shape=labels.shape)
        labels[rr, cc] = n

    return labels


"""
#excluded=['JVASP-76418','JVASP-76611','JVASP-19999','JVASP-76567','JVASP-652','JVASP-6379','JVASP-60567','JVASP-60331','JVASP-8981','JVASP-8984','JVASP-60475','JVASP-31368','JVASP-75366','JVASP-75078','JVASP-60353','JVASP-27957','JVASP-6346','JVASP-676','JVASP-76604']
excluded=['JVASP-60433']
my_data=[]
for i in data("dft_2d"):
    if i['jid'] not in excluded and len(my_data)<129:
        my_data.append(i)

"""
# my_data = data("dft_2d")[0:128]


class Jarvis2dSTEMDataset:
    """Simulated STEM dataset (jarvis dft_2d)"""

    def __init__(
        self,
        px_scale: float = 0.1,
        label_mode: str = "delta",
        image_data: Optional[List[Dict[str, Any]]] = None,
        rotation_degrees: Optional[float] = None,
        shift_angstrom: Optional[float] = None,
        zoom_pct: Optional[float] = None,
        to_tensor: Optional[Callable] = None,
    ):
        """Simulated STEM dataset, jarvis-2d data

        px_scale: pixel size in angstroms
        label_mode: `delta` or `radius`, controls atom localization mask style

        ## augmentation settings
        rotation_degrees: if specified, sample from Unif(-rotation_degrees, rotation_degrees)
        shift_angstrom: if specified, sample from Unif(-shift_angstrom, shift_angstrom)
        zoom_pct: optional image scale factor: s *= 1 + (zoom_pct/100)

        """

        if label_mode not in LABEL_MODES:
            raise NotImplementedError(f"label mode {label_mode} not supported")

        self.px_scale = px_scale
        self.label_mode = label_mode
        self.to_tensor = to_tensor

        self.rotation_degrees = rotation_degrees
        self.shift_angstrom = shift_angstrom
        self.zoom_pct = zoom_pct

        if image_data is not None:
            self.df = pd.DataFrame(image_data)
        else:
            self.df = pd.DataFrame(data("dft_2d"))

        self.stem = STEMConv(output_size=[256, 256])

        train_ids, val_ids, test_ids = self.split_dataset()
        self.train_ids = train_ids
        self.val_ids = val_ids
        self.test_ids = test_ids

    def split_dataset(self, val_frac: float = 0.1, test_frac: float = 0.1):
        N = len(self.df)
        n_val = int(N * val_frac)
        n_test = int(N * test_frac)
        n_train = N - (n_val + n_test)

        # set a consistent train/val/test split
        torch.manual_seed(0)
        shuf = torch.randperm(N)
        torch.random.seed()
        train_ids = shuf[:n_train].tolist()
        val_ids = shuf[n_train : n_train + n_val].tolist()
        test_ids = shuf[n_train + n_val : n_train + n_val + n_test].tolist()

        return train_ids, val_ids, test_ids

    def __len__(self):
        """Datset size: len(jarvis_2d)"""
        return self.df.shape[0]

    def __getitem__(self, idx):
        """Sample: image, label mask, atomic coords, numbers, structure ids."""
        row = self.df.iloc[idx]
        # print (row.jid)
        a = Atoms.from_dict(row.atoms)

        # defaults:
        rot = 0
        shift_x = 0
        shift_y = 0
        px_scale = self.px_scale

        # apply pre-rendering structure augmentation
        if self.rotation_degrees is not None:
            rot = np.random.uniform(-self.rotation_degrees, self.rotation_degrees)

        if self.shift_angstrom is not None:
            shift_x, shift_y = np.random.uniform(
                -self.shift_angstrom, self.shift_angstrom, size=2
            )

        if self.zoom_pct is not None:
            frac = self.zoom_pct / 100
            px_scale *= 1 + np.random.uniform(-frac, frac)

        image, label, pos, nb = self.stem.simulate_surface(
            a, px_scale=px_scale, eps=0.6, rot=rot, shift=[shift_x, shift_y]
        )

        if self.label_mode == "radius":
            label = atomic_radius_mask(image.shape, pos, nb, px_scale)

        if self.to_tensor is not None:
            image = self.to_tensor(torch.tensor(image))

        sample = {
            "image": image,
            "label": torch.FloatTensor(label > 0),
            "id": row.jid,
            "px_scale": px_scale,
            "crys": row.crys,
        }

        # sample = {"image": image, "label": label, "coords": pos, "id": row.jid}
        return sample


def atom_mask_to_graph(label, image, px_angstrom=0.1, cutoff_angstrom=4):
    """Construct attributed atomistic graph from foreground mask

    px_angstrom: pixel size in angstrom

    Performs connected component analysis on label image
    Computes region properties (centroids, radius, mean intensity)
    Constructs a radius graph of atoms within `cutoff` (px)
    """
    g = nx.Graph()
    g.graph["px_angstrom"] = px_angstrom

    # connected component analysis
    rlab = measure.label(label)

    # per-atom-detection properties for node attributes
    props = pd.DataFrame(
        measure.regionprops_table(
            rlab,
            intensity_image=image / image.max(),
            properties=[
                "label",
                "centroid",
                "equivalent_diameter",
                "min_intensity",
                "mean_intensity",
                "max_intensity",
            ],
        )
    )

    # add nodes with attributes to graph
    for id, row in props.iterrows():
        # px * angstrom/px -> angstrom
        pos = np.array([row["centroid-1"], row["centroid-0"], 0]) * px_angstrom
        eq_radius = 0.5 * row.equivalent_diameter * px_angstrom
        g.add_node(id, pos=pos, intensity=row.mean_intensity, r=eq_radius)

    # construct radius graph edges via kd-tree
    points = props.loc[:, ("centroid-1", "centroid-0")].values * px_angstrom
    nbrs = KDTree(points)
    g.add_edges_from(nbrs.query_pairs(cutoff_angstrom))

    return g, props


def bond_vectors(edges):
    """Compute bond displacement vectors from pairwise atom coordinates."""
    u = edges.src["pos"]
    v = edges.dst["pos"]
    return {"r": v - u}


class Jarvis2dSTEMGraphDataset(Jarvis2dSTEMDataset):
    """Simulated STEM dataset (jarvis dft_2d): graph encoding"""

    def __init__(
        self,
        px_scale: float = 0.1,
        label_mode: str = "delta",
        image_data: Optional[List[Dict[str, Any]]] = None,
        to_tensor: Optional[Callable] = None,
        pixel_classifier=None,
        debug=False,
    ):
        """Simulated STEM dataset, jarvis-2d data

        px_scale: pixel size in angstroms
        label_mode: `delta` or `radius`, controls atom localization mask style


        debug: use ground truth label annotations

        Running the pixel classifier like this in the dataloader is not the most efficient
        It might be viable to put this inside a closure used for a dataloader collate_fn
        This would assemble the full batch, run the pixel classifier, and then construct
        the atomistic graphs. Hopefully this is all done during DataLoader prefetch still.

        Depending on the quality of the label predictions, this could potentially need
        label smoothing as well.


        example: initialize GCN-only ALIGNN with two atom input features
        cfg = alignn.ALIGNNConfig(name="alignn", alignn_layers=0, atom_input_features=2)
        model = alignn.ALIGNN(cfg)
        model(g)
        """
        super().__init__(
            px_scale=px_scale, label_mode=label_mode, image_data=image_data
        )
        self.pixel_classifier = pixel_classifier
        self.debug = debug

    def __getitem__(self, idx):
        """Sample: image, label mask, atomic coords, numbers, structure ids."""
        sample = super().__getitem__(idx)

        if self.debug:
            predicted_label = sample["label"]
        else:
            predicted_label = self.pixel_classifier(sample["image"])

        g, props = atom_mask_to_graph(predicted_label, sample["image"])
        g = dgl.from_networkx(g, node_attrs=["pos", "intensity", "r"])

        # compute bond vectors from atomic coordinates
        # store results in g.edata["r"]
        g.apply_edges(bond_vectors)

        # unit conversion: -> angstrom
        # px * angstrom/px -> angstrom
        g.edata["r"] = (g.edata["r"] * self.px_scale).type(torch.float32)

        # coalesce atom features
        h = torch.stack((g.ndata["intensity"], g.ndata["r"]), dim=1)
        g.ndata["atom_features"] = h.type(torch.float32)

        sample["g"] = g

        return sample
