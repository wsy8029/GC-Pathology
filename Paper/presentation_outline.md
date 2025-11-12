# GreenVet 조직검사 데이터 발표자료 구성안

## Page 1. 2024년 데이터 개요
- 2024년 전달받은 조직검사 메타데이터는 48,692행, 16,943건의 고유 검사 의뢰 번호, 16,949개의 고유 슬라이드 식별자로 구성되어 있음.
- 의뢰당 행 수는 평균 2.87행(중앙값 2행)이며, 75% 분위수 4행, 최대 18행으로 다중 슬라이드/스냅샷 구조를 강조할 필요가 있음.
- 서비스 유형 분포: `Histopathology (1 Site/Lesion)-국내` 38,581건(79.2%), `Histopathology (2 Site/Lesion)-국내` 7,946건(16.3%), `Histopathology (3 Site/Lesion)-국내` 1,777건(3.6%), `Histopathology (4 Site/Lesion)-국내` 388건(0.8%).
- 위치 코드(`site1`~`site4`) 활용 가능성 강조: `site1` 43,465건(89.3%), `site2` 4,446건(9.1%), `site3` 686건(1.4%), `site4` 95건(0.2%).

| 구분 | 건수 | 비율 |
| --- | ---: | ---: |
| 전체 행 수 | 48,692 | 100.0% |
| 고유 검사 의뢰 수 | 16,943 | — |
| 고유 슬라이드 ID | 16,949 | — |

## Page 2. 검사 의뢰별 진단 텍스트 수 분포
- 고유 진단 텍스트가 1개인 의뢰가 15,362건(90.7%)으로 대부분 단일 진단으로 보고됨.
- 2개 이상의 진단이 공존하는 의뢰는 1,581건(9.3%)이며, 최대 5개까지 기록된 사례가 존재함.

| 고유 진단 텍스트 수 | 의뢰 건수 | 비율 |
| ---: | ---: | ---: |
| 1 | 15,362 | 90.7% |
| 2 | 1,397 | 8.2% |
| 3 | 164 | 1.0% |
| 4 | 18 | 0.1% |
| 5 | 2 | 0.0% |

## Page 3. 진단명 빈도 (상위 및 장기 꼬리)
- 상위 5개 진단은 지방종, 유선 복합선종, 삼차모세포종, 피지선 선종, 피부조직구종 순으로 분포함.
- 희소 진단명도 다수 존재하여, 1,625개의 진단명이 2회씩, 1,374개의 진단명이 3회씩 등장하는 등 장기 꼬리가 매우 김.

| 순위 | 진단명 | 건수 |
| ---: | --- | ---: |
| 1 | Subcutaneous lipoma | 1,198 |
| 2 | Mammary complex adenoma, completely excised | 739 |
| 3 | Trichoblastoma, completely excised | 734 |
| 4 | Sebaceous adenoma, completely excised | 613 |
| 5 | Cutaneous histiocytoma, completely excised | 546 |

| 등장 빈도 | 해당 진단명 수 |
| ---: | ---: |
| 1회 | 1 |
| 2회 | 1,625 |
| 3회 | 1,374 |
| 4회 | 1,487 |
| 5회 | 228 |
| 6회 | 248 |
| 7회 | 125 |
| 8회 | 149 |
| 9회 | 71 |
| 10회 | 83 |

## Page 4. 진단명 표기의 불일치 사례
- 동일 개념의 병변이라도 수술 경계, 다중 병변 표기, 언어 혼용 등으로 진단명이 상이하게 기록되어 있음.
- 아래 사례는 Vet-ICD-O 매핑을 위해 텍스트 정규화가 필요한 대표 패턴을 제시함.

| 대표 개념 | 원본 표기 예시 |
| --- | --- |
| Subcutaneous lipoma | `Subcutaneous lipoma`, `Subcutaneous lipoma, completely excised`, `Lipoma, completely excised`, `Subcutaneous lipoma with inflammation` |
| Mammary complex adenoma | `Mammary complex adenoma, completely excised`, `Mammary complex adenoma`, `Mammary complex adenoma, close margin`, `Carcinoma arising in a mammary complex adenoma (Low-grade)` |
| Cutaneous mast cell tumor | `Cutaneous mast cell tumor (Low-grade / Grade 2)`, `Cutaneous mast cell tumor (Low-grade / Grade 2), completely excised`, `Cutaneous mast cell tumor, low grade, margin incomplete`, `Subcutaneous mast cell tumor` |

## Page 5. 텍스트 정규화 및 Vet-ICD-O 매핑 전략
- 정규화 대상: `DIAGNOSIS`, `GROSS_FINDINGS`, `MICROSCOPIC_FINDINGS`, `COMMENTS` 4개 컬럼.
- 절차 제안
  1. HTML 태그 제거, 언어별 토크나이징, 불필요 공백/특수문자 정제.
  2. Vet-ICD-O 사전 구축(C코드 Topography, 8xxx Morphology) 및 동의어 확장.
  3. 규칙 기반 정규표현식 + 퍼지 매칭 + 멀티링구얼 임베딩을 이용한 다단계 후보 탐색.
  4. 매칭 신뢰도 스코어링과 수동 검증 큐를 통해 품질 관리.
- 정규화 결과를 신규 컬럼(`Vet-ICD-O_Topography`, `Vet-ICD-O_Morphology`, `Specimen_Site_Normalized`, `Species`)로 확장하여 Vet-ICD-O 규격 기반 데이터셋 구축.

## Page 6. Vet-ICD-O
- Vet-ICD-O는 사람 대상 ICD-O 체계를 수의병리 환경에 맞춰 확장한 분류체계로, 해부 부위(Topography)와 형태학적 진단(Morphology)을 동물 종별로 표준화함.
- 수의과 종 특화 코드셋으로 동물 종에 맞춘 병변 분류 및 통계 보고, 병변 맵핑, 다기관 데이터 상호운용성 확보에 활용 가능.
- 프로젝트에서는 Vet-ICD-O canine 1판을 기준으로 코드 사전을 구성하고, 정규화된 텍스트와 매핑하여 다중 컬럼을 생성함.

## Page 7. 정규화 결과 예시 (샘플 데이터)
- 정규화 및 코드 매핑 후 생성될 데이터 예시를 표로 제시하여 이해를 돕는다.

| INSP_RQST_NO | DIAGNOSIS | Vet-ICD-O_Topography | Vet-ICD-O_Morphology | Specimen_Site_Normalized | Species |
| --- | --- | --- | --- | --- | --- |
| 20240101-113-0002 | Trichoblastoma, completely excised | C44.9 (Skin, NOS) | 8100/0 (Trichoblastoma) | Skin, left hindlimb flank | Cat |
| 20240101-121-0003 | Subcutaneous lipoma, completely excised | C49.2 (Connective tissue, lower limb) | 8850/0 (Lipoma) | Perigenital subcutis/skin | — |
| 20240101-102-0001 | Mammary carcinoma (Low-grade), completely excised | C50.9 (Breast, NOS) | 8520/3 (Infiltrating duct carcinoma) | Mammary chain, multifocal | Cat |
| 20240101-119-0002 | 섬유종(Fibroma) | C44.9 (Skin, NOS) | 8810/0 (Fibroma, keloidal) | Skin nodule, unspecified site | — |
| 20240102-134-0002 | Comment 참조 | C44.9 (Skin, NOS) | N/A granulomatous dermatitis (Granulomatous dermatitis (non-neoplastic)) | Skin mass with granulomatous inflammation | — |

## Page 8. Vet-ICD-O 정규화의 기대 효과
- 진단 텍스트와 해부 부위, 종 정보를 구조화하여 병리 보고서의 일관성과 검색 편의성을 향상.
- SNOMED, ICD 계열 코드와의 매핑 확장을 위한 기초 데이터 제공.
- 다기관 비교 연구, 품질 관리, 자동 보고서 생성 등 후속 AI 파이프라인과의 연동 기반 마련.
- Vet-ICD-O 매핑 과정에서 `completely excised`와 같은 임상적 디테일이 누락되지 않도록 별도의 보존 컬럼을 병행 관리해야 함.

## Page 9. Downstream Task 로드맵
- GreenVet 메타데이터와 WSI가 제공하는 멀티모달 요소를 활용하여 다음과 같은 시나리오 추진 가능.
  1. **슬라이드 수준 질환 분류 및 트리아지**: MIL 기반 양성/악성 판별, 종양 유형 분류, 재검 권장 여부 예측. CLAM/TOAD, HIPT, Virchow2, UNI/CONCH 등의 파운데이션 모델 활용. 추가 예후 라벨 확보 시 리스크 예측으로 확장.
  2. **병소 탐지 및 병변 정량화**: U-Net/HoVer-Net, Segment Anything, Virchow2·UNI 임베딩을 통한 약지도 히트맵 등으로 병리사 주석 부담 완화.
  3. **보고서 생성·QA 및 대화형 지원**: HistoGPT, PathChat, PathAsst, Med-PaLM Multimodal 등 비전-언어 모델 활용으로 보고서 초안 생성과 품질 검증 자동화.
  4. **멀티모달 검색 및 지식 그래프**: PathFoundation, Slide2Graph, CPathCLIP 등으로 병리 패턴·키워드 기반 검색 및 교육 자료화.
  5. **운영 자동화 및 워크플로 통합**: NVIDIA Holoscan, MONAI Deploy 등으로 QA 자동화, 스캐닝 오류 감지, LIS/PACS 연동 추진.
- 단기 실행 안: 우선 한 가지 진단명을 선정해 양성/악성 구분 이진 분류 모델(MIL 또는 패치 기반)을 구축하는 실험 설계 중.

## Page 10. 논의 사항 (Discussion)
- **Task/Process 효율성**: GreenVet 및 일반 조직병리 워크플로의 병목 지점 파악, 자동화 후보(Task 관리, 슬라이드 스캐닝, 보고서 검수 등) 정의 필요.
- **신규 서비스/BM**: GreenVet의 장기 비전과 유사 산업군 벤치마킹을 기반으로 한 프리미엄 병리 리포트, 교육/데이터 서비스, AI 지원 진단 상품 구상.

