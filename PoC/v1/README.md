# WSI 양성/악성 분류 PoC (GIGAPATH 우선)

그린벳 WSI 데이터로 양성/악성 분류 PoC를 수행하는 과정과 현재 상태를 정리합니다. 임베딩은 병리 특화 Foundation Model **GIGAPATH**를 1순위로 사용하고, 필요 시 UNI/CONCH를 대안으로 고려합니다.

## 1. 최근 진행 결과 요약
- **데이터/라벨**: `mammary_adenoma_vs_adenocarcinoma_only(2023).parquet` 라벨(`mammary_adenoma`, `mammary_adenocarcinoma`) 기준. 라벨 정규화 시 `adenoma/adenocarcinoma` 및 `mammary_adenoma/mammary_adenocarcinoma` 모두 매핑.
- **특징량 소스**: `s3://gc-pathology/gv-level1-embedding/` 의 `.pt` 파일 48건을 RunPod로 동기화. PT 내부 키는 `layer_*_embed`만 존재해, 가장 높은 레이어를 자동 선택하는 로더로 수정.
- **학습 설정**: TransMIL + positional encoding, fp16, max_tiles=8000, epochs=20, cosine LR, 자동 pos_weight. 좌표가 있으면 사용, 없으면 순수 임베딩 MIL.
- **로그/출력**(원격): `/workspace/data/logs/mil_training.json`, `val_preds.csv`, `test_preds.csv`. 로컬에서 7번 셀 실행 시 원격 성능 요약을 출력.
- **현재 병목**: 데이터 수(48 슬라이드)와 클래스 불균형/라벨 노이즈 가능성 → 일반화 한계가 크며 AUC/F1은 참고치로만 활용.

## 2. 데이터 파이프라인
1. **라벨 업로드**: 로컬 parquet → RunPod `/workspace/data/work/`.
2. **S3 임베딩 동기화**: `RUN_S3_SYNC=True` 시 `.pt` 다운로드 → `/workspace/data/work/features/`.
3. **Manifest 생성**: 라벨 stem(`FILE_NAME`, `INSP_RQST_NO`)과 PT stem(`*_gigapath` 제거)을 매칭, train/val/test split 기록.
4. **학습/로그**: `train_mil.py`를 원격에 기록 후 학습. 결과는 `/workspace/data/logs`에 저장.

## 3. 모델/학습 상세
- **입력 로더**: `load_feats_coords`가 `features/feats/embedding/last_layer_embed` 또는 `layer_<n>_embed` 중 가장 높은 레이어를 선택. 좌표 키(`coords/coord/locs/locations`)가 있으면 pos enc 적용.
- **헤드**: TransMIL(기본), 대안으로 AttentionMIL/CLAM 선택 가능.
- **손실/스케줄**: BCEWithLogits + pos_weight 자동 추정, AdamW + cosine, fp16 GradScaler.
- **평가**: val/test split에 대해 AUC/AP/F1/ACC, 혼동행렬을 JSON 저장, 예측 CSV 별도 저장.

## 4. 실행 방법 (RunPod 노트북 `gigapath_runpod_modeling.ipynb`)
1. 셀 2→4→6→8→10 순으로 실행해 환경/데이터 준비.
2. 셀 12 실행해 학습 스크립트 갱신.
3. 셀 14에서 `RUN_TRAINING=True`로 설정 후 학습 실행.
4. 셀 7(성능 평가) 실행 시 원격 로그 요약 출력. 로그 없으면 먼저 학습 필요.

## 5. 현재 성능 해석 및 한계
- **샘플 수 48**로 매우 적고, 라벨 불확실성 존재 → AUC/F1는 변동성이 크며 과적합 위험이 높음.
- PT에 좌표가 없는 경우 순수 임베딩 MIL로만 학습되어 위치 정보 활용이 제한됨.
- 임베딩 키가 `layer_*_embed`만 있는 특이 구조 → 로더가 자동 선택하지만, 임베딩 추출 단계가 표준화되지 않았을 수 있음.

## 6. 향후 개선 방향
1. **데이터 확충/검증**
   - 슬라이드 수 확대 및 클래스 밸런싱, 라벨 재검수.
   - 좌표 포함 임베딩 재생성(가능 시)으로 pos enc 효과 확인.
2. **모델/학습**
   - Light 정규화: label smoothing, dropout/weight decay 튜닝, max_tiles 축소로 안정화.
   - 다른 MIL 헤드(AttentionMIL/CLAM) 비교, threshold 최적화.
3. **평가**
   - 추가 val/test 슬라이드로 재분할, k-fold cross-validation 권장.
   - RunPod 로그를 주기적으로 확인(노트북 7번 셀)하고 JSON/CSV를 로컬에 보관.
4. **추론/데모**
   - 학습 완료된 체크포인트로 heatmap 추출 스크립트 추가.
   - 간단한 Gradio/Streamlit 뷰어에 heatmap/슬라이드 ID별 확률 표시.

## 7. 참고 리소스
- GIGAPATH, UNI, CONCH 원본 리포와 논문 (병리 특화 임베딩).
- MIL 구현 예제: TransMIL, CLAM, ABMIL.
- 추론 최적화: ONNX Runtime, TensorRT, faiss 캐싱.
