try:
    from rdkit import Chem
    from qm9.rdkit_functions import BasicMolecularMetrics
    use_rdkit = True
except ModuleNotFoundError:
    use_rdkit = False

import os
import copy
import glob
import torch
import subprocess
from tqdm import tqdm
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.stats as sp_stats
from Bio.PDB import PDBParser

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from qm9 import bond_analyze
import qm9.dataset as dataset
from analysis.metrics import rdmol_to_smiles

# code borrowed from DiffSBDD
# https://github.com/arneschneuing/DiffSBDD/tree/main/analysis
from analysis.metrics import BasicMolecularMetrics as DiffSBDD_MolecularMetrics
from analysis.metrics import MoleculeProperties
from analysis.molecule_builder import build_molecule



# 'atom_decoder': ['H', 'B', 'C', 'N', 'O', 'F', 'Al', 'Si', 'P', 'S', 'Cl', 'As', 'Br', 'I', 'Hg', 'Bi'],

analyzed_19 ={'atom_types': {1: 93818, 3: 21212, 0: 139496, 2: 8251, 4: 26},
            'distances': [0, 0, 0, 0, 0, 0, 0, 22566, 258690, 16534, 50256, 181302, 19676, 122590, 23874, 54834, 309290, 205426, 172004, 229940, 193180, 193058, 161294, 178292, 152184, 157242, 189186, 150298, 125750, 147020, 127574, 133654, 142696, 125906, 98168, 95340, 88632, 80694, 71750, 64466, 55740, 44570, 42850, 36084, 29310, 27268, 23696, 20254, 17112, 14130, 12220, 10660, 9112, 7640, 6378, 5350, 4384, 3650, 2840, 2362, 2050, 1662, 1414, 1216, 966, 856, 492, 516, 420, 326, 388, 326, 236, 140, 130, 92, 62, 52, 78, 56, 24, 8, 10, 12, 18, 2, 10, 4, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
}


class Histogram_discrete:
    def __init__(self, name='histogram'):
        self.name = name
        self.bins = {}

    def add(self, elements):
        for e in elements:
            if e in self.bins:
                self.bins[e] += 1
            else:
                self.bins[e] = 1

    def normalize(self):
        total = 0.
        for key in self.bins:
            total += self.bins[key]
        for key in self.bins:
            self.bins[key] = self.bins[key] / total

    def plot(self, save_path=None):
        width = 1  # the width of the bars
        fig, ax = plt.subplots()
        x, y = [], []
        for key in self.bins:
            x.append(key)
            y.append(self.bins[key])

        ax.bar(x, y, width)
        plt.title(self.name)
        if save_path is not None:
            plt.savefig(save_path)
        else:
            plt.show()
        plt.close()


class Histogram_cont:
    def __init__(self, num_bins=100, range=(0., 13.), name='histogram', ignore_zeros=False):
        self.name = name
        self.bins = [0] * num_bins
        self.range = range
        self.ignore_zeros = ignore_zeros

    def add(self, elements):
        for e in elements:
            # ~!fp16
            if not self.ignore_zeros or e > 1e-8:
                i = int(float(e) / self.range[1] * len(self.bins))
                i = min(i, len(self.bins) - 1)
                self.bins[i] += 1

    def plot(self, save_path=None):
        width = (self.range[1] - self.range[0])/len(self.bins)                 # the width of the bars
        fig, ax = plt.subplots()

        x = np.linspace(self.range[0], self.range[1], num=len(self.bins) + 1)[:-1] + width / 2
        ax.bar(x, self.bins, width)
        plt.title(self.name)

        if save_path is not None:
            plt.savefig(save_path)
        else:
            plt.show()
        plt.close()


    def plot_both(self, hist_b, save_path=None, wandb=None):
        ## TO DO: Check if the relation of bins and linspace is correct
        hist_a = normalize_histogram(self.bins)
        hist_b = normalize_histogram(hist_b)

        #width = (self.range[1] - self.range[0]) / len(self.bins)  # the width of the bars
        fig, ax = plt.subplots()
        x = np.linspace(self.range[0], self.range[1], num=len(self.bins) + 1)[:-1]
        ax.step(x, hist_b)
        ax.step(x, hist_a)
        ax.legend(['True', 'Learned'])
        plt.title(self.name)

        if save_path is not None:
            plt.savefig(save_path)
            if wandb is not None:
                if wandb is not None:
                    # Log image(s)
                    im = plt.imread(save_path)
                    wandb.log({save_path: [wandb.Image(im, caption=save_path)]})
        else:
            plt.show()
        plt.close()


def normalize_histogram(hist):
    hist = np.array(hist)
    prob = hist / np.sum(hist)
    return prob


def coord2distances(x):
    x = x.unsqueeze(2)
    x_t = x.transpose(1, 2)
    dist = (x - x_t) ** 2
    dist = torch.sqrt(torch.sum(dist, 3))
    dist = dist.flatten()
    return dist


def earth_mover_distance(h1, h2):
    p1 = normalize_histogram(h1)
    p2 = normalize_histogram(h2)
    distance = sp_stats.wasserstein_distance(p1, p2)
    return distance


def kl_divergence(p1, p2):
    return np.sum(p1*np.log(p1 / p2))


def kl_divergence_sym(h1, h2):
    # ~!fp16
    p1 = normalize_histogram(h1) + 1e-10
    p2 = normalize_histogram(h2) + 1e-10
    kl = kl_divergence(p1, p2)
    kl_flipped = kl_divergence(p2, p1)
    return (kl + kl_flipped) / 2.


def js_divergence(h1, h2):
    # ~!fp16
    p1 = normalize_histogram(h1) + 1e-10
    p2 = normalize_histogram(h2) + 1e-10
    M = (p1 + p2)/2
    js = (kl_divergence(p1, M) + kl_divergence(p2, M)) / 2
    return js


def main_analyze_qm9(remove_h: bool, dataset_name='qm9', n_atoms=None):
    class DataLoaderConfig(object):
        def __init__(self):
            self.batch_size = 128
            self.remove_h = remove_h
            self.filter_n_atoms = n_atoms
            self.num_workers = 0
            self.include_charges = True
            self.dataset = dataset_name  #could be qm9, qm9_first_half or qm9_second_half
            self.datadir = 'qm9/temp'
            self.permutation_file_path = None
            self.data_file = None

    cfg = DataLoaderConfig()

    dataloaders, charge_scale = dataset.retrieve_dataloaders(cfg)

    hist_nodes = Histogram_discrete('Histogram # nodes')
    hist_atom_type = Histogram_discrete('Histogram of atom types')
    hist_dist = Histogram_cont(name='Histogram relative distances', ignore_zeros=True)

    for i, data in enumerate(dataloaders['train']):
        print(i * cfg.batch_size)

        # Histogram num_nodes
        num_nodes = torch.sum(data['atom_mask'], dim=1)
        num_nodes = list(num_nodes.numpy())
        hist_nodes.add(num_nodes)

        #Histogram edge distances
        x = data['positions'] * data['atom_mask'].unsqueeze(2)
        dist = coord2distances(x)
        hist_dist.add(list(dist.numpy()))

        # Histogram of atom types
        one_hot = data['one_hot'].double()
        atom = torch.argmax(one_hot, 2)
        atom = atom.flatten()
        mask = data['atom_mask'].flatten()
        masked_atoms = list(atom[mask].numpy())
        hist_atom_type.add(masked_atoms)

    hist_dist.plot()
    hist_dist.plot_both(hist_dist.bins[::-1])
    print("KL divergence A %.4f" % kl_divergence_sym(hist_dist.bins, hist_dist.bins[::-1]))
    print("KL divergence B %.4f" % kl_divergence_sym(hist_dist.bins, hist_dist.bins))
    print(hist_dist.bins)
    hist_nodes.plot()
    print("Histogram of the number of nodes", hist_nodes.bins)
    hist_atom_type.plot()
    print(" Histogram of the atom types (H (optional), C, N, O, F)", hist_atom_type.bins)


############################
# Validity and bond analysis
def check_stability(positions, atom_type, dataset_info, debug=False):
    assert len(positions.shape) == 2
    assert positions.shape[1] == 3
    atom_decoder = dataset_info['atom_decoder']
    x = positions[:, 0]
    y = positions[:, 1]
    z = positions[:, 2]

    nr_bonds = np.zeros(len(x), dtype='int')

    for i in range(len(x)):
        for j in range(i + 1, len(x)):
            p1 = np.array([x[i], y[i], z[i]])
            p2 = np.array([x[j], y[j], z[j]])
            dist = np.sqrt(np.sum((p1 - p2) ** 2))
            atom1, atom2 = atom_decoder[atom_type[i]], atom_decoder[atom_type[j]]
            pair = sorted([atom_type[i], atom_type[j]])
            if dataset_info['name'] == 'qm9' or dataset_info['name'] == 'qm9_second_half' or dataset_info['name'] == 'qm9_first_half':
                order = bond_analyze.get_bond_order(atom1, atom2, dist)
            elif dataset_info['name'] == 'geom' or "ligand" in dataset_info['name'].lower():
                order = bond_analyze.geom_predictor(
                    (atom_decoder[pair[0]], atom_decoder[pair[1]]), dist)
            nr_bonds[i] += order
            nr_bonds[j] += order
    nr_stable_bonds = 0
    for atom_type_i, nr_bonds_i in zip(atom_type, nr_bonds):
        try:
            possible_bonds = bond_analyze.allowed_bonds[atom_decoder[atom_type_i]]
        except KeyError:  # CA_only (AA not found in Allowed Bonds between atoms)
            return False, nr_stable_bonds, len(x)
        if type(possible_bonds) == int:
            is_stable = possible_bonds == nr_bonds_i
        else:
            is_stable = nr_bonds_i in possible_bonds
        if not is_stable and debug:
            print("Invalid bonds for molecule %s with %d bonds" % (atom_decoder[atom_type_i], nr_bonds_i))
        nr_stable_bonds += int(is_stable)

    molecule_stable = nr_stable_bonds == len(x)
    return molecule_stable, nr_stable_bonds, len(x)


def process_loader(dataloader):
    """ Mask atoms, return positions and atom types"""
    out = []
    for data in dataloader:
        for i in range(data['positions'].size(0)):
            positions = data['positions'][i].view(-1, 3)
            one_hot = data['one_hot'][i].view(-1, 5).type(torch.float32)
            mask = data['atom_mask'][i].flatten()
            positions, one_hot = positions[mask], one_hot[mask]
            atom_type = torch.argmax(one_hot, dim=1)
            out.append((positions, atom_type))
    return out


def main_check_stability(remove_h: bool, batch_size=32):
    from configs import datasets_config
    import qm9.dataset as dataset

    class Config:
        def __init__(self):
            self.batch_size = batch_size
            self.num_workers = 0
            self.remove_h = remove_h
            self.filter_n_atoms = None
            self.datadir = 'qm9/temp'
            self.dataset = 'qm9'
            self.include_charges = True
            self.filter_molecule_size = None
            self.sequential = False
            self.permutation_file_path = None
            self.data_file = None

    cfg = Config()

    dataset_info = datasets_config.qm9_with_h
    dataloaders, charge_scale = dataset.retrieve_dataloaders(cfg)
    if use_rdkit:
        from qm9.rdkit_functions import BasicMolecularMetrics
        metrics = BasicMolecularMetrics(dataset_info)

    atom_decoder = dataset_info['atom_decoder']

    def test_validity_for(dataloader):
        count_mol_stable = 0
        count_atm_stable = 0
        count_mol_total = 0
        count_atm_total = 0
        for [positions, atom_types] in dataloader:
            is_stable, nr_stable, total = check_stability(
                positions, atom_types, dataset_info)

            count_atm_stable += nr_stable
            count_atm_total += total

            count_mol_stable += int(is_stable)
            count_mol_total += 1

            print(f"Stable molecules "
                  f"{100. * count_mol_stable/count_mol_total:.2f} \t"
                  f"Stable atoms: "
                  f"{100. * count_atm_stable/count_atm_total:.2f} \t"
                  f"Counted molecules {count_mol_total}/{len(dataloader)*batch_size}")

    train_loader = process_loader(dataloaders['train'])
    test_loader = process_loader(dataloaders['test'])
    if use_rdkit:
        print('For test')
        metrics.evaluate(test_loader)
        print('For train')
        metrics.evaluate(train_loader)
    else:
        print('For train')
        test_validity_for(train_loader)
        print('For test')
        test_validity_for(test_loader)


def compute_molecule_metrics(molecule_list, dataset_info):
    one_hot = molecule_list['one_hot']
    x = molecule_list['x']
    node_mask = molecule_list['node_mask']

    if isinstance(node_mask, torch.Tensor):
        atomsxmol = torch.sum(node_mask, dim=1)
    else:
        atomsxmol = [torch.sum(m) for m in node_mask]

    n_samples = len(x)

    molecule_stable = 0
    nr_stable_bonds = 0
    n_atoms = 0

    processed_list = []

    for i in range(n_samples):
        atom_type = one_hot[i].argmax(1).cpu().detach()
        pos = x[i].cpu().detach()

        atom_type = atom_type[0:int(atomsxmol[i])]
        pos = pos[0:int(atomsxmol[i])]
        processed_list.append((pos, atom_type))

    for mol in processed_list:
        pos, atom_type = mol
        validity_results = check_stability(pos, atom_type, dataset_info)

        molecule_stable += int(validity_results[0])
        nr_stable_bonds += int(validity_results[1])
        n_atoms += int(validity_results[2])

    # Stability
    fraction_mol_stable = molecule_stable / float(n_samples)
    fraction_atm_stable = nr_stable_bonds / float(n_atoms)
    
    if use_rdkit:
        # validity, uniquness, novelty
        metrics = BasicMolecularMetrics(dataset_info)
        rdkit_metrics = metrics.evaluate(processed_list)
    else:
        rdkit_metrics = None
    
    # Other metrics referenced from DiffSBDD
    # convert into rdmols
    # rdmols = [build_molecule(pos, atom_type, dataset_info) \
    #           for (pos, atom_type) in processed_list]
    rdmols = []
    for pos, atom_type in processed_list:
        try:
            built_molecule = build_molecule(pos, atom_type, dataset_info)
            if built_molecule is not None:
                rdmols.append(built_molecule)
        except Exception as e:
            print(f"Failed to build molecule: {e}")

    # won't be computing novelty & uniqueness with 
    # this, no need for dataset SMILES list.
    ligand_metrics = DiffSBDD_MolecularMetrics(dataset_info, dataset_smiles_list=None)
    molecule_properties = MoleculeProperties()
    
    # filter valid molecules
    valid_mols, _ = ligand_metrics.compute_validity(rdmols)
    
    # compute connectivity
    connected_mols, connectivity, _ = \
            ligand_metrics.compute_connectivity(valid_mols)
    
    # other basic metrics
    qed, sa, logp, lipinski, diversity = \
        molecule_properties.evaluate_mean(connected_mols)

    metrics_dict = {
        'validity': rdkit_metrics[0][0] if use_rdkit else None,
        'uniqueness': rdkit_metrics[0][1] if use_rdkit else None,
        'novelty': rdkit_metrics[0][2] if use_rdkit else None,
        'mol_stable': fraction_mol_stable,
        'atm_stable': fraction_atm_stable,
        'connectivity': connectivity,
        'QED': qed,
        'SA': sa,
        'logP': logp,
        'lipinski': lipinski,
        'diversity': diversity
    }
    
    return metrics_dict


def compute_qvina2_score(
        molecule_list, 
        dataset_info, 
        pocket_ids=[], 
        pocket_pdb_dir="", 
        output_dir="",
        mgltools_env_name="mgltools-python2",
        connectivity_thres=1.,
        ligand_add_H=False,
        receptor_add_H=False,
        remove_nonstd_resi=False,
        size=20,
        exhaustiveness=16,
        seed=42,
        cleanup_files=True,
        save_csv=True
    ):
    one_hot = molecule_list['one_hot']
    x = molecule_list['x']
    node_mask = molecule_list['node_mask']

    if isinstance(node_mask, torch.Tensor):
        atomsxmol = torch.sum(node_mask, dim=1)
    else:
        atomsxmol = [torch.sum(m) for m in node_mask]

    n_samples = len(x)

    processed_list = []
    for i in range(n_samples):
        atom_type = one_hot[i].argmax(1).cpu().detach()
        pos = x[i].cpu().detach()

        atom_type = atom_type[0:int(atomsxmol[i])]
        pos = pos[0:int(atomsxmol[i])]
        processed_list.append((pos, atom_type))
    
    # filter molecules
    rdmols = []
    rdmols_ids = []
    for i, (pos, atom_type) in enumerate(processed_list):
        try:
            # build RDKit molecule
            mol = build_molecule(pos, atom_type, dataset_info)
        except Exception as e:
            print(f"Failed to build molecule: {e}")
            continue
        
        if mol is not None:
            # filter valid molecules
            try:
                Chem.SanitizeMol(mol)
            except ValueError:
                continue
            
            if mol is not None:
                # filter connected molecules
                mol_frags = Chem.rdmolops.GetMolFrags(mol, asMols=True)
                largest_mol = \
                    max(mol_frags, default=mol, key=lambda m: m.GetNumAtoms())
                if largest_mol.GetNumAtoms() / mol.GetNumAtoms() >= connectivity_thres:
                    smiles = rdmol_to_smiles(largest_mol)
                    if smiles is not None:
                        rdmols.append(largest_mol)
                        rdmols_ids.append(pocket_ids[i])
    
    # compute qvina scores for each pocket-ligand pair
    scores = []
    results = {
        'receptor': [],
        'ligands': [],
        'scores': []
    }
    for i in tqdm(range(len(rdmols))):
        
        id = str(rdmols_ids[i]).zfill(7)
        
        pattern = os.path.join(pocket_pdb_dir, f'{id}*.pdb')
        pkt_file = glob.glob(pattern)
        assert len(pkt_file) <= 1
        
        if len(pkt_file) == 0:
            print(f">>> [qm9.analyze.compute_qvina_score] Pocket ID: {id} not found")
            continue
        
        lg_sdf_file   = Path(output_dir, f"{id}.sdf")
        lg_pdb_file   = Path(output_dir, f"{id}.pdb")
        lg_pdbqt_file = Path(output_dir, f"{id}.pdbqt")
        pkt_pdb_file  = Path(pkt_file[0])
        pkt_pdbqt_file = Path(output_dir, f"{pkt_pdb_file.stem}.pdbqt")
        qvina_out_file = Path(output_dir, f"{id}_qvina.txt")
        
        
        # Move ligand's center (x,y,z) to pocket's center (x,y,z) so that it falls in 
        # the bounding box search space in order for qvina2.1 to work
        pocket_center = get_pocket_center(str(pkt_pdb_file))
        # Center ligand around (0, 0, 0) first
        mol_trans = copy.deepcopy(rdmols[i])
        mol_trans, ligand_original_center = center_ligand(mol_trans)
        # Translate ligand to the pocket center
        mol_trans = translate_ligand_to_pocket_center(mol_trans, pocket_center)
        pocket_center = pocket_center.tolist()
        cx, cy, cz = float(pocket_center[0]), float(pocket_center[1]), float(pocket_center[2])
        
        
        # LG: .sdf
        with Chem.SDWriter(str(lg_sdf_file)) as writer:
            writer.write(mol_trans)
            # writer.write(rdmols[i])
        
        # LG: .pdb
        os.popen(f'obabel {lg_sdf_file} -O {lg_pdb_file}').read()
        
        # LG: .pdbqt (add charges and torsions)
        # subprocess.run(prep_lg_cmd, shell=True)
        cd_cmd = f"cd {os.path.dirname(lg_pdb_file)}"
        prep_lg_cmd = f"{cd_cmd} && conda run -n {mgltools_env_name} prepare_ligand4.py -l {os.path.basename(lg_pdb_file)} -o {os.path.basename(lg_pdbqt_file)}"
        prep_lg_cmd += " -A hydrogens" if ligand_add_H else ""
        subprocess.run(prep_lg_cmd, shell=True)
        
        # PKT: .pdbqt
        prep_pkt_cmd = f"conda run -n {mgltools_env_name} prepare_receptor4.py -r {pkt_pdb_file} -o {pkt_pdbqt_file}"
        # prep_pkt_cmd += " -A checkhydrogens" if receptor_add_H else ""
        prep_pkt_cmd += " -A hydrogens" if receptor_add_H else ""
        prep_pkt_cmd += " -e" if remove_nonstd_resi else ""
        subprocess.run(prep_pkt_cmd, shell=True)

        # run QuickVina 2
        qvina_cmd = \
            f'./analysis/qvina/qvina2.1 --receptor {pkt_pdbqt_file} ' + \
            f'--ligand {lg_pdbqt_file} ' + \
            f'--center_x {cx:.4f} --center_y {cy:.4f} --center_z {cz:.4f} ' + \
            f'--size_x {size} --size_y {size} --size_z {size} ' + \
            f'--exhaustiveness {exhaustiveness} ' + \
            f'--seed {seed}'
        print(qvina_cmd)
        out = os.popen(qvina_cmd).read()
        print(out)
        with open(str(qvina_out_file), 'w') as f:
            print(out, file=f)

        if '-----+------------+----------+----------' not in out:
            scores.append(np.nan)
            continue

        out_split = out.splitlines()
        best_idx = out_split.index('-----+------------+----------+----------') + 1
        best_line = out_split[best_idx].split()
        assert best_line[0] == '1'
        best_score = float(best_line[1])
        scores.append(best_score)

        results['receptor'].append(str(pkt_pdb_file))
        results['ligands'].append(str(lg_sdf_file))
        results['scores'].append(best_score)
        
        # clean up
        if cleanup_files:
            lg_pdb_file.unlink()
            lg_pdbqt_file.unlink()
            pkt_pdbqt_file.unlink()
    
    filtered_scores = [score for score in scores if not np.isnan(score)]
    scores_average = sum(filtered_scores) / len(filtered_scores) if filtered_scores else float('nan')
    
    if save_csv:
        results_save = copy.deepcopy(results)
        results_save['receptor'].append("")
        results_save['ligands'].append("Mean Score")
        results_save['scores'].append([scores_average])
        df = pd.DataFrame.from_dict(results_save)
        df.to_csv(Path(output_dir, 'qvina2_scores.csv'))
    
    return {
        'mean': scores_average,
        'all': scores,
        'results': results
    }


def get_pocket_center(pdb_file):
    """Calculate the geometric center of a pocket from a PDB file."""
    parser = PDBParser(QUIET=True)
    structure = parser.get_structure("pocket", pdb_file)
    coordinates = []

    # Extract atomic coordinates
    for atom in structure.get_atoms():
        coordinates.append(atom.coord)

    # Calculate the geometric center
    coordinates = np.array(coordinates)
    center = np.mean(coordinates, axis=0)
    print(f">>> Pocket center: {center}")
    
    return center

def center_ligand(ligand):
    """Center the ligand coordinates around (0, 0, 0)."""
    conf = ligand.GetConformer()
    coords = np.array([list(conf.GetAtomPosition(i)) for i in range(ligand.GetNumAtoms())])

    # Calculate ligand's center
    ligand_center = np.mean(coords, axis=0)
    print(f">>> Ligand center beforehand: {ligand_center}")

    # Translate coordinates to center the ligand at (0, 0, 0)
    for i in range(ligand.GetNumAtoms()):
        conf.SetAtomPosition(i, coords[i] - ligand_center)
    
    return ligand, ligand_center

def translate_ligand_to_pocket_center(ligand, pocket_center):
    """Translate the ligand to align its center with the pocket center and display the new center."""
    # Get the conformer (3D coordinates) of the ligand
    conf = ligand.GetConformer()
    
    # Iterate over each atom in the ligand
    for i in range(ligand.GetNumAtoms()):
        # Get the current position of the atom
        current_pos = np.array(conf.GetAtomPosition(i))
        
        # Translate the atom position by adding the pocket center (move the ligand)
        conf.SetAtomPosition(i, current_pos + pocket_center)

    # Calculate the new center of the ligand after translation
    new_center = np.mean([np.array(conf.GetAtomPosition(i)) for i in range(ligand.GetNumAtoms())], axis=0)
    
    # Display the new center (xyz coordinates)
    print(f">>> Ligand center after: {new_center}")
    
    # Return the modified ligand with updated positions
    return ligand



def analyze_node_distribution(mol_list, save_path):
    hist_nodes = Histogram_discrete('Histogram # nodes (stable molecules)')
    hist_atom_type = Histogram_discrete('Histogram of atom types')

    for molecule in mol_list:
        positions, atom_type = molecule
        hist_nodes.add([positions.shape[0]])
        hist_atom_type.add(atom_type)
    print("Histogram of #nodes")
    print(hist_nodes.bins)
    print("Histogram of # atom types")
    print(hist_atom_type.bins)
    hist_nodes.normalize()



if __name__ == '__main__':

    # main_analyze_qm9(remove_h=False, dataset_name='qm9')
    main_check_stability(remove_h=False)

