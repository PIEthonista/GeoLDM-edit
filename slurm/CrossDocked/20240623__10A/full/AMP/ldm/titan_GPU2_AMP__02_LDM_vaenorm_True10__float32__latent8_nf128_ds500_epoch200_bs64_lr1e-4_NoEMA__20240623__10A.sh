#! /bin/bash -l

#SBATCH --partition=gpu-titan
#SBATCH --ntasks=32
#SBATCH --nodes=1
#SBATCH --mem=100G
#SBATCH --gpus=2
#SBATCH --job-name=AMP__02_LDM_vaenorm_True10__float32__latent8_nf128_ds500_epoch200_bs64_lr1e-4_NoEMA__20240623__10A
#SBATCH --output=slurm_out/AMP__02_LDM_vaenorm_True10__float32__latent8_nf128_ds500_epoch200_bs64_lr1e-4_NoEMA__20240623__10A.out
#SBATCH --error=slurm_err/AMP__02_LDM_vaenorm_True10__float32__latent8_nf128_ds500_epoch200_bs64_lr1e-4_NoEMA__20240623__10A.err
#SBATCH --qos=long
#SBATCH --hint=multithread
#SBATCH --mail-type=ALL
#SBATCH --mail-user=gohyixian456@gmail.com

# 119932
# module load cuda/12.1       # gpu-a100
# module load miniconda/24.1.2
# conda activate geoldm-a100


# module load cuda/cuda-11.8  # gpu-v100s
# module load miniconda/miniconda3
# conda activate geoldm


module load cuda/cuda-11.8  # gpu-titan
module load miniconda/24.1.2
conda activate geoldm

cd /home/user/yixian.goh/geoldm-edit
python check_gpu.py
python main_geom_drugs.py --config_file configs/model_configs/CrossDocked/20240623__10A/full/AMP/ldm/best/AMP__02_LDM_vaenorm_True10__float32__latent8_nf128_ds500_epoch200_bs64_lr1e-4_NoEMA__20240623__10A.yaml