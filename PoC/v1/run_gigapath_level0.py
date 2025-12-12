#!/usr/bin/env python3
"""
RunPod MIL experiment using level-0 Gigapath embeddings.
This is a standalone version of the gigapath_runpod_modeling notebook configured
to pull features from s3://gc-pathology/gv-level0-embedding-metadata/.
"""

import json
import shlex
import subprocess
from pathlib import Path

# SSH / RunPod settings
SSH_HOST_GATEWAY = "vvy5ke98vgtmim-64411852@ssh.runpod.io"
SSH_HOST_DIRECT = "root@69.30.85.100"
SSH_PORT_DIRECT = 22027
SSH_KEY = "~/.ssh/runpod_peter"
USE_DIRECT_FOR_SSH = True
USE_DIRECT_FOR_RSYNC = True
SSH_EXTRA_OPTS = ""

# Remote paths (dedicated level-0 workspace)
REMOTE_BASE = "/workspace/data"
REMOTE_WORK = f"{REMOTE_BASE}/work_level0"
REMOTE_FEATURE_DIR = f"{REMOTE_WORK}/features"
REMOTE_CHECKPOINT = f"{REMOTE_WORK}/checkpoints"
REMOTE_LOG = f"{REMOTE_WORK}/logs"
REMOTE_MANIFEST = f"{REMOTE_WORK}/manifest_level0.csv"
REMOTE_TRAIN_SCRIPT = f"{REMOTE_WORK}/train_mil.py"
REMOTE_VENV = "~/venv_gigapath"

# S3 settings (level-0 embeddings)
S3_PREFIX = "s3://gc-pathology/gv-level0-embedding-metadata/"
AWS_PROFILE = None
AWS_REGION = "ap-northeast-2"
AWS_ENDPOINT = None
RUN_S3_SYNC = True

# Label / split settings
REPO_ROOT = Path(__file__).resolve().parent
LABEL_PARQUET_LOCAL = REPO_ROOT / "mammary_adenoma_vs_adenocarcinoma_only(2023).parquet"
LABEL_MAP = {"adenoma": 0, "adenocarcinoma": 1, "mammary_adenoma": 0, "mammary_adenocarcinoma": 1}
VAL_RATIO = 0.2
TEST_RATIO = 0.0  # keep everything in train/val for this small run
RANDOM_SEED = 42
MAX_TILES_PER_SLIDE = 4000

# Training toggles
RUN_REMOTE_SETUP = True
RUN_TRAINING = True

# Training hyperparameters
EPOCHS = 40
LR = 1e-4
GRAD_ACCUM = 2
HIDDEN_DIM = 256
PATIENCE = 8
PRECISION = "fp16"
MODEL = "abmil"  # choices: abmil | clam | transmil

# Local output (logs fetched from RunPod)
LOCAL_OUTPUT = REPO_ROOT / "output" / "level0"


def run_local(cmd: str, check: bool = True):
    print(f"[local] $ {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Local command failed ({result.returncode}): {cmd}{msg}")
    return result


def _ssh_parts(host: str, port: int = None):
    parts = ["ssh"]
    if SSH_KEY:
        parts += ["-i", SSH_KEY]
    if SSH_EXTRA_OPTS:
        parts += shlex.split(SSH_EXTRA_OPTS)
    if port:
        parts += ["-p", str(port)]
    parts.append(host)
    return parts


def run_ssh(cmd: str, check: bool = True, use_direct: bool = None):
    if use_direct is None:
        use_direct = USE_DIRECT_FOR_SSH
    parts = _ssh_parts(SSH_HOST_DIRECT if use_direct else SSH_HOST_GATEWAY, SSH_PORT_DIRECT if use_direct else None)
    ssh_cmd = " ".join(shlex.quote(p) for p in parts + [cmd])
    print(f"[ssh] $ {cmd}")
    result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)
    if check and result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"SSH command failed ({result.returncode}): {cmd}{msg}")
    return result


def _rsync_ssh_opt(use_direct: bool = True):
    key = Path(SSH_KEY).expanduser() if SSH_KEY else None
    parts = ["ssh"]
    if key:
        parts += ["-i", str(key)]
    if SSH_EXTRA_OPTS:
        parts += shlex.split(SSH_EXTRA_OPTS)
    if use_direct:
        parts += ["-p", str(SSH_PORT_DIRECT)]
    return " ".join(parts)


def rsync_upload(local_path: Path, remote_dir: str):
    use_direct = USE_DIRECT_FOR_RSYNC
    ssh_opt = _rsync_ssh_opt(use_direct)
    remote_host = SSH_HOST_DIRECT if use_direct else SSH_HOST_GATEWAY
    remote_target = f"{remote_host}:{remote_dir.rstrip('/')}/"
    cmd = (
        f"rsync -avh --no-perms --no-owner --no-group --partial --progress -e {shlex.quote(ssh_opt)} "
        f"{shlex.quote(str(local_path))} {remote_target}"
    )
    run_local(cmd)


def rsync_download(remote_path: str, local_dir: Path):
    use_direct = USE_DIRECT_FOR_RSYNC
    ssh_opt = _rsync_ssh_opt(use_direct)
    remote_host = SSH_HOST_DIRECT if use_direct else SSH_HOST_GATEWAY
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_src = f"{remote_host}:{remote_path}"
    cmd = (
        f"rsync -avh --no-perms --no-owner --no-group --partial --progress -e {shlex.quote(ssh_opt)} "
        f"{remote_src} {shlex.quote(str(local_dir))}/"
    )
    run_local(cmd)


def ensure_remote_dirs():
    print("[단계] 원격 디렉터리 준비 및 GPU 상태 확인")
    run_ssh(f"mkdir -p {REMOTE_FEATURE_DIR} {REMOTE_CHECKPOINT} {REMOTE_LOG} {REMOTE_WORK}")
    run_ssh("echo 'SSH OK on $(hostname)' && nvidia-smi || true", check=False)
    run_ssh("df -h . || true", check=False)
    print("[완료] 원격 디렉터리 준비 완료")


REMOTE_PY_PKGS = [
    "pip",
    "setuptools",
    "wheel",
    "pandas",
    "numpy",
    "scikit-learn",
    "tqdm",
    "torchmetrics",
    "einops",
    "pyyaml",
    "awscli",
    "pyarrow",
    "timm",
    "openslide-python",
    "huggingface_hub",
    "matplotlib",
    "scikit-image",
    "scipy",
    "pillow",
]


def install_remote_deps(pkgs=None, venv_path: str = REMOTE_VENV):
    pkgs = pkgs or REMOTE_PY_PKGS
    pkg_str = " ".join(pkgs)
    cmd = (
        f"python3 -m venv {venv_path} && "
        f"{venv_path}/bin/pip install --upgrade pip setuptools wheel && "
        f"{venv_path}/bin/pip install {pkg_str}"
    )
    print("[단계] 가상환경 설치/업데이트 시작 ->", venv_path)
    print("       패키지:", pkg_str)
    run_ssh(cmd)
    print("[완료] 가상환경 패키지 설치 완료")


def sync_s3_features():
    env_exports = []
    if AWS_PROFILE:
        env_exports.append(f"AWS_PROFILE={AWS_PROFILE}")
    if AWS_REGION:
        env_exports.append(f"AWS_DEFAULT_REGION={AWS_REGION}")
    if AWS_ENDPOINT:
        env_exports.append(f"AWS_ENDPOINT_URL={AWS_ENDPOINT}")
    env_prefix = " ".join(env_exports) + " " if env_exports else ""
    cmd = f"{env_prefix}{REMOTE_VENV}/bin/aws s3 sync {S3_PREFIX} {REMOTE_FEATURE_DIR}/"
    print(f"[단계] S3 임베딩 동기화 시작: {S3_PREFIX} -> {REMOTE_FEATURE_DIR}")
    run_ssh(cmd)
    stat_cmd = (
        f"echo '[remote] PT 파일 개수:' && find {REMOTE_FEATURE_DIR} -maxdepth 1 -name '*.pt' | wc -l && "
        f"du -sh {REMOTE_FEATURE_DIR} || true; "
        f"ls -1 {REMOTE_FEATURE_DIR} | head || true; "
        f"ls -1 {REMOTE_FEATURE_DIR} | tail || true"
    )
    run_ssh(stat_cmd, check=False)
    print("[완료] S3 동기화 완료")


def upload_labels():
    if not LABEL_PARQUET_LOCAL.exists():
        raise FileNotFoundError(f"label parquet not found: {LABEL_PARQUET_LOCAL}")
    print(f"[단계] 라벨 parquet 업로드: {LABEL_PARQUET_LOCAL.name} -> {REMOTE_WORK}")
    rsync_upload(LABEL_PARQUET_LOCAL, REMOTE_WORK)
    print("[완료] 라벨 업로드 완료")


def build_remote_manifest():
    from string import Template

    print("[단계] 원격 manifest 생성 시작")
    tmpl = Template(
        r"""
$VENV/bin/python - <<'PY'
import json, random
from collections import Counter
from pathlib import Path
import subprocess

try:
    import pandas as pd
except ImportError:
    print('[단계] pandas/pyarrow 미설치 → 설치 시도')
    subprocess.check_call(['$VENV/bin/pip', 'install', '-q', 'pandas', 'pyarrow'])
    import pandas as pd

label_path = Path('$REMOTE_WORK') / '$LABEL_NAME'
features_dir = Path('$FEATURE_DIR')
manifest_path = Path('$MANIFEST')
label_map = $LABEL_MAP_JSON
val_ratio = $VAL_RATIO
test_ratio = $TEST_RATIO
seed = $SEED

print('[단계] manifest 생성')
print(' label parquet:', label_path)
print(' feature dir  :', features_dir)
print(' split 비율 -> val', val_ratio, 'test', test_ratio)

if not label_path.exists():
    raise SystemExit(f'label parquet missing on remote: {label_path}')
if not features_dir.exists():
    raise SystemExit(f'feature dir missing: {features_dir}')

df = pd.read_parquet(label_path)

label_col = None
for cand in ['label', 'LABEL']:
    if cand in df.columns:
        label_col = cand
        break
if label_col is None:
    raise SystemExit('label column not found; expected \"label\"')

def norm_label(x):
    key = str(x).lower().strip()
    return label_map.get(key)

def candidate_ids(row):
    ids = []
    file_names = str(row.get('FILE_NAME', '')).split('|')
    for fn in file_names:
        fn = fn.strip()
        if fn:
            ids.append(Path(fn).stem)
    slide_id = row.get('INSP_RQST_NO', row.get('INSP_RQST_NUM', row.get('FOLDER', '')))
    slide_id = str(slide_id).strip()
    if slide_id:
        ids.append(slide_id)
    return [i for i in ids if i]

label_index = {}
for _, row in df.iterrows():
    lbl = norm_label(row[label_col])
    if lbl is None:
        continue
    for cid in candidate_ids(row):
        label_index[cid] = int(lbl)

print('라벨 인덱스 매핑 개수:', len(label_index))

feature_files = sorted(features_dir.glob('*.pt'))
print('임베딩 파일 개수:', len(feature_files))

records = []
unmatched = []
for pt in feature_files:
    slide_id = pt.stem.replace('_gigapath', '')
    lbl = label_index.get(slide_id)
    if lbl is None:
        unmatched.append(slide_id)
        continue
    records.append({'slide_id': slide_id, 'label': lbl, 'pt_path': str(pt.resolve())})

if not records:
    raise SystemExit('No features matched labels; check naming conventions.')

random.seed(seed)
random.shuffle(records)

val_n = int(len(records) * val_ratio)
test_n = int(len(records) * test_ratio)
if len(records) >= 2 and val_n == 0:
    val_n = 1
if len(records) - val_n - test_n <= 0:
    test_n = 0
if len(records) - val_n <= 0 and len(records) > 1:
    val_n = len(records) - 1
if len(records) - val_n <= 0:
    raise SystemExit('Not enough samples to split into train/val.')

for i, rec in enumerate(records):
    if i < val_n:
        rec['split'] = 'val'
    elif i < val_n + test_n:
        rec['split'] = 'test'
    else:
        rec['split'] = 'train'

man_df = pd.DataFrame.from_records(records)
print('매칭된 라벨 분포:', dict(Counter(man_df['label'])))
print('split 개수:', man_df['split'].value_counts().to_dict())
man_df.to_csv(manifest_path, index=False)
print('manifest 저장 완료:', manifest_path)
if unmatched:
    print('라벨 매칭 실패 feature (앞 10개):', unmatched[:10])
PY
"""
    )
    script = tmpl.substitute(
        VENV=REMOTE_VENV,
        REMOTE_WORK=REMOTE_WORK,
        LABEL_NAME=LABEL_PARQUET_LOCAL.name,
        FEATURE_DIR=REMOTE_FEATURE_DIR,
        MANIFEST=REMOTE_MANIFEST,
        LABEL_MAP_JSON=json.dumps({k: int(v) for k, v in LABEL_MAP.items()}),
        VAL_RATIO=VAL_RATIO,
        TEST_RATIO=TEST_RATIO,
        SEED=RANDOM_SEED,
    )
    run_ssh(script)
    print("[완료] 원격 manifest 생성 완료 ->", REMOTE_MANIFEST)


def upload_train_script():
    src = REPO_ROOT / "train_mil.py"
    if not src.exists():
        raise FileNotFoundError(f"train_mil.py not found at {src}")
    print(f"[단계] 학습 스크립트 업로드 -> {REMOTE_TRAIN_SCRIPT}")
    rsync_upload(src, REMOTE_WORK)
    run_ssh(f"chmod +x {REMOTE_TRAIN_SCRIPT}")
    print("[완료] 학습 스크립트 업로드 완료")


def run_training():
    train_cmd = (
        f"{REMOTE_VENV}/bin/python {REMOTE_TRAIN_SCRIPT} "
        f"--manifest {REMOTE_MANIFEST} "
        f"--ckpt_dir {REMOTE_CHECKPOINT} "
        f"--logdir {REMOTE_LOG} "
        f"--model {MODEL} "
        f"--max_tiles {MAX_TILES_PER_SLIDE} "
        f"--epochs {EPOCHS} "
        f"--lr {LR} "
        f"--grad_accum {GRAD_ACCUM} "
        f"--precision {PRECISION} "
        f"--hidden_dim {HIDDEN_DIM} "
        f"--patience {PATIENCE} "
        f"--num_workers 2 "
        f"--threshold 0.5 "
    )
    print("[단계] 학습 실행 커맨드:", train_cmd)
    run_ssh(train_cmd)


def fetch_logs():
    LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
    print("[단계] 로그 파일 다운로드 ->", LOCAL_OUTPUT)
    rsync_download(f"{REMOTE_LOG}/", LOCAL_OUTPUT)
    log_path = LOCAL_OUTPUT / "mil_training.json"
    if log_path.exists():
        data = json.loads(log_path.read_text())
        print("[결과] best_val_auc:", data.get("best_val_auc"))
        print(" val metrics :", data.get("val"))
        print(" test metrics:", data.get("test"))
    else:
        print("[경고] mil_training.json 없음; 원격 로그를 확인하세요.")


def main():
    print("== RunPod level-0 MIL 실험 시작 ==")
    print("S3 prefix:", S3_PREFIX)
    print("Remote feature dir:", REMOTE_FEATURE_DIR)
    print("Label parquet:", LABEL_PARQUET_LOCAL)
    ensure_remote_dirs()
    if RUN_REMOTE_SETUP:
        install_remote_deps()
    else:
        print("RUN_REMOTE_SETUP=False -> venv 설치 건너뜀")
    if RUN_S3_SYNC:
        sync_s3_features()
    else:
        print("RUN_S3_SYNC=False -> S3 동기화 건너뜀")
    upload_labels()
    build_remote_manifest()
    upload_train_script()
    if RUN_TRAINING:
        run_training()
    else:
        print("RUN_TRAINING=False -> 학습 건너뜀")
    fetch_logs()
    print("== 완료 ==")


if __name__ == "__main__":
    main()
