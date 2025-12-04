# PoC v2 — TITAN 기반 경로 및 테스트 플로우

## 왜 TITAN으로 가나?
- **임베딩 분리력 한계**: GigaPath 사전학습 임베딩으로는 adenoma vs adenocarcinoma 분리가 약해 MIL 로그잇이 0.4~0.6에 몰리고 AUC가 0.53~0.64 수준에서 정체.
- **라벨/슬라이드 매칭·품질 우려**: manifest 자동 매칭 과정에 slide_id 중복/규칙(# 포함) 노이즈 가능성이 있어 학습 신호 혼탁.
- **불균형·타일 노이즈**: 약 1.7:1 클래스 비율, pos_weight 없이 학습하면 FP 쏠림. 타일에 배경/저신뢰가 섞여 신호 대비 노이즈 비율이 높음.
- **모델 복잡도 대비 데이터 크기**: TransMIL/CLAM/ABMIL 모두 입력 신호 자체가 약해 큰 폭 개선 없음.
- **대안**: 2025 SOTA **TITAN**(CONCH v1.5 patch + TITAN slide encoder)로 임베딩 품질을 끌어올리고, 동일 MIL/heatmap 파이프라인에 투입해 재평가.

## 목표
- RunPod `/workspace` 환경에서 **TITAN patch→slide 임베딩**을 소수 슬라이드에 대해 smoke test.
- 결과물: `*.h5`(CONCH patch features + coords + patch_size_level0), `*.pt`(slide embedding), 썸네일, 좌표 CSV, 히트맵 PNG.
- 기존 v1 데이터/라벨을 재사용하되, v2 산출물은 `/workspace/PoC/v2`에만 저장하여 혼합 방지.

## 디렉터리
- `PoC/v2/TITAN/` — TITAN 깃 리포 clone.
- `PoC/v2/TITAN_test.ipynb` — RunPod에서 실행할 smoke test 노트북.
- `PoC/v2/output/` — 테스트 결과 저장 위치(슬라이드별 하위 폴더 생성 권장).

## RunPod 실행 요약 (/workspace 기준)
```bash
# 0) 리포 가져오기 (이미 로컬에 있다면 /workspace로 복사)
cd /workspace
# 예시: git clone 또는 rsync로 가져오기

# 1) Conda/venv 준비 (예시: conda)
conda create -n titan-poc python=3.10 -y
conda activate titan-poc

# 2) 핵심 패키지 설치 (GPU 이미지라면 CUDA 빌드 선택)
pip install --upgrade pip
# CUDA 12.1 예시: (이미지에 맞는 torch/cu 버전 선택)
pip install torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu121
pip install timm==1.0.3 einops==0.6.1 einops-exts==0.0.4 tqdm==4.66.6 \
    h5py==3.8.0 transformers==4.46.0 pandas==2.2.3 scikit-learn==1.5.2 \
    matplotlib seaborn opencv-python openslide-python huggingface_hub==0.26.5

# 3) TITAN 설치
pip install -e /workspace/PoC/v2/TITAN

# 4) Hugging Face 로그인 (모델 접근 필요)
python - <<'PY'
from huggingface_hub import login
login()  # 개인 access token 입력
PY

# 5) 노트북 실행
cd /workspace/PoC/v2
jupyter notebook  # 혹은 jupyter lab, VSCode 등
```

## 테스트 플로우(TITAN_test.ipynb에 구현)
1) **입력 설정**: `WSI_LIST`, `DATA_ROOT`를 /workspace의 SVS 위치로 지정. level 1 기준 패치 크기 256px(default) → level0 patch_size=256×downsample(대개 512).
2) **모델 로드**: `AutoModel.from_pretrained("MahmoodLab/TITAN", trust_remote_code=True)` 후 `return_conch()`로 CONCH v1.5 patch encoder + transform 획득.
3) **타일링(레벨1)**: `openslide`로 백그라운드 필터링하며 stride=patch_size_lv1로 슬라이드 전 영역 스캔. 좌표는 level0 단위로 저장.
4) **CONCH 특징 추출**: 배치 단위 FP16 inference로 patch feature 추출 → `features.h5`에 `features`, `coords`, `patch_size_level0` 저장.
5) **TITAN 슬라이드 임베딩**: `encode_slide_from_patch_features(features, coords, patch_size_lv0)` → `slide_embedding.pt` 저장.
6) **썸네일·히트맵**: level1 썸네일 생성 후, patch feature와 slide embedding의 cosine similarity를 rasterize하여 heatmap PNG 저장. 좌표 CSV/JSON도 함께 기록.
7) **산출물 정리**: `/workspace/PoC/v2/output/<slide_id>/`에 h5, pt, thumbnail.png, heatmap.png, coords.csv.

## 메모
- Patch coords는 level0 기준이어야 하며, `patch_size_level0`를 h5 attr로 기록해야 TITAN에서 stride를 올바로 인식.
- smoke test는 슬라이드 소수(2~3개)로 돌려 GPU/시간 사용 최소화 후, 전체 배치로 확장.
- 히트맵은 patch feature vs slide embedding cosine-sim 기반 간이 시각화로, 이후 MIL 결과 overlay 시 좌표/썸네일을 재사용 가능.
