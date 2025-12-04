# 병리 데이터 라벨링·모델링 PoC 개요

그린벳으로부터 받은 데이터는 **WSI(SVS) 이미지**와 **병리사가 자유롭게 적은 findings/diagnosis 엑셀**뿐이었습니다. 과거 뷰노 방식은 병리사가 ROI를 직접 지정하며 라벨링해야 해 비용·시간이 많이 들었습니다. 이번 PoC는 **ROI 없이도 빠르게 라벨을 만들고, Foundation Model로 바로 학습/시각화까지 진행하는 흐름을 증명**하는 데 초점을 둡니다.

---

## 한눈에 보는 흐름(텍스트 라벨 → 임베딩 → 모델/시각화)
```
[Findings/Diagnosis 자유 텍스트]
          │  (LLM 기반 정규화/라벨 매핑)
          ▼
[구조화 라벨: mammary_adenoma / mammary_adenocarcinoma]
          │
          ├─(CSV/Parquet 저장: mammary_adenoma_vs_adenocarcinoma*.{csv,parquet})
          │
[WSI(SVS)] ──(GIGAPATH 임베딩 추출)──▶ [슬라이드별 Feature Vector] ──┐
                                                                    │
                      ┌───────────────────────────────┐            │
                      │ Heatmap 시각화 (`heatmap.ipynb`) │◀────────┘
                      └───────────────────────────────┘
                                       │
                                       ▼
                  [MIL 기반 양성/악성 분류 실험 (`gigapath_mammary_adenoma_vs_carcinoma.ipynb`)]
```

---

## 무엇을 해결했나
- **ROI 없는 자동 라벨링**: 자유 텍스트 findings/diagnosis를 AI로 정규화해 `mammary_adenoma` vs `mammary_adenocarcinoma` 라벨을 자동 생성. 병리사가 ROI를 직접 그릴 필요 없음.
- **Foundation Model 바로 활용**: 2024년 SOTA **GIGAPATH**로 슬라이드 임베딩(특징 벡터) 추출, 히트맵 생성까지 완료. 2025년 SOTA **TITAN** 모델로도 동일 흐름을 테스트 예정.
- **모델링 착수**: 생성된 라벨과 임베딩으로 **MIL 기반 양성/악성 분류 모델**을 학습·평가 중. 짧은 기간에 라벨링→임베딩→모델링까지 연결됨을 검증.
- **재현 가능한 도구 세트**: 모든 단계가 노트북으로 정리돼 있어, 신규 데이터나 다른 병변/모달리티에도 바로 적용 가능.

---

## 세부 흐름과 사용 노트북
- **텍스트 라벨링/전처리**: `preprocessing.ipynb`  
  자유 텍스트 → 정규화 라벨(`mammary_adenoma`, `mammary_adenocarcinoma`) 생성, CSV/Parquet 저장.
- **GIGAPATH 임베딩 추출**: `gigapath*.ipynb`, `gigapath_runpod*.ipynb`  
  WSI → Foundation Model 임베딩(Feature Vector) 생성.
- **히트맵/시각화**: `heatmap.ipynb`  
  임베딩 기반으로 슬라이드 내 관심 영역을 히트맵으로 표현.
- **양성/악성 분류 실험**: `gigapath_mammary_adenoma_vs_carcinoma.ipynb`  
  MIL 기반 분류 모델 학습/평가로 실제 대체 가능성 확인.

---

## 왜 의미 있는가
- **라벨링 비용·시간 절감**: ROI 수작업 없이도 라벨이 자동 생성되어 병리사 리소스 투입 최소화.
- **빠른 PoC 반복**: 텍스트 정규화 + Foundation Model 임베딩만으로 수주 단위 PoC 완성, 추가 가설을 빠르게 검증 가능.
- **대체 가능성 확인**: 과거 ROI 기반 워크플로우의 핵심 단계를 **Foundation Model + 자동 라벨링**으로 대체할 수 있음을 실제 데이터로 입증.
- **확장성**: 동일한 접근을 다른 병변, 다른 모달리티(예: GC지놈의 시퀀스 데이터)에도 적용해볼 수 있는 기반 확보.

---

## 현재 상태와 다음 단계
- GIGAPATH 기반 임베딩·히트맵 완료, 분류 모델 초기 학습/평가 진행 중.
- **TITAN** 모델을 동일 파이프라인에 적용해 성능·재현성 비교 예정.
- 라벨 품질 강화 및 데이터 수 확장 시, 임상적으로 의미 있는 수준까지 성능 향상 기대. 필요 시 ROI 없이도 부분적 해석(heatmap) 제공 가능.
