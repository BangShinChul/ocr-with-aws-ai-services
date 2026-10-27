# OCR with AWS AI Services — 문서 판독 정확도 PoC 데모

스캔한 문서 한 장을 Amazon Textract, Amazon Bedrock(Claude·GPT), Amazon Bedrock Data Automation(BDA)으로 각각 처리해서, **사람이 확인한 정답과 필드별로 비교한 정확도·처리 시간·비용**을 보여주는 도구입니다.

- 고객 PC에서 실행하고 **고객 AWS 계정의 API를 호출**합니다. 서버나 웹 서비스를 배포하지 않습니다.
- 문서는 해당 AWS 계정 밖으로 나가지 않습니다. 이미지는 S3를 거치지 않고, PDF와 BDA만 비공개 S3 버킷을 씁니다.
- 화면은 `127.0.0.1`(실행한 PC)에서만 열립니다. 로그인 기능이 없으므로 외부에 공개하지 마세요.

> 원본: [aws-samples/ocr-with-aws-ai-services](https://github.com/aws-samples/ocr-with-aws-ai-services) (MIT-0). 최신 Claude·GPT 모델, 토큰 기준 비용 기록, 정답 비교 일괄 평가, 사람 검수 화면, 설치 스크립트를 추가했습니다.

## 준비물

- Python 3.10 이상
- AWS CLI 프로필 (`aws configure` 또는 `aws configure sso`로 설정)
- 해당 계정에서 Amazon Bedrock의 Claude 모델 사용이 가능해야 합니다. GPT 모델까지 비교하려면 OpenAI 모델 사용도 가능해야 합니다(처음 호출할 때 AWS Marketplace 구독 권한이 필요할 수 있습니다). 필요한 IAM 권한 예시는 [`docs/iam-policy-example.json`](docs/iam-policy-example.json)에 있습니다.

## 빠른 시작

### 1. 설치와 점검

```bash
# macOS / Linux
./setup.sh --profile <AWS 프로필> --region ap-northeast-2
```
```powershell
# Windows PowerShell
.\setup.ps1 --profile <AWS 프로필> --region ap-northeast-2
```

이 스크립트는 다음을 처리합니다.

1. `.venv` 가상환경을 만들고 고정 버전 패키지를 설치합니다.
2. 프로필의 계정 ID를 보여줍니다. **PoC용 계정이 맞는지 꼭 확인하세요.**
3. Bedrock 모델(Sonnet·Haiku·Opus 5.5), Textract, BDA를 실제로 한 번씩 호출해 봅니다. 비용은 1센트 미만입니다.
4. 결과를 `.env`에 저장합니다.

PDF를 처리하거나 BDA를 쓰려면 비공개 S3 버킷이 필요합니다.

```bash
./setup.sh --profile <프로필> --region <리전> --create-bucket      # 새로 만들기 (생성 전에 확인을 받습니다)
./setup.sh --profile <프로필> --region <리전> --bucket <기존 버킷>  # 있는 버킷 쓰기
```

`--create-bucket`으로 만드는 버킷은 다음처럼 설정됩니다.

- 퍼블릭 액세스 차단 4개 항목 모두 켬
- ACL 사용 안 함
- HTTPS가 아닌 요청 거부
- 업로드한 객체는 7일 뒤 자동 삭제

BDA는 일부 리전에서만 제공됩니다. 서울(ap-northeast-2)에서는 점검 결과가 "unavailable"로 나오며, 이 경우 BDA만 꺼지고 나머지는 그대로 동작합니다.

### 2. 문서와 정답 준비

| 위치 | 내용 | 누가 |
|---|---|---|
| `data/images/<이름>.png` | 시험할 스캔본 (PNG·JPEG·TIFF·PDF) | 고객 |
| `data/schema/<이름>.json` | 뽑을 항목과 표기 규칙, 검산 규칙 | 고객 (예시: [`examples/flight-log.schema.json`](examples/flight-log.schema.json)) |
| `data/truth/<이름>.json` | **원본을 보고 사람이 확정한 정답** | 고객 정비/업무 담당자 |

```bash
./run.sh template scan_01.png      # 스키마를 바탕으로 비어 있는 정답 파일을 만듭니다
```

정답 파일 작성 시 알아 두실 점:

- 정답 파일의 키 구조는 스키마와 같아야 합니다.
- 값을 `null`로 두면 그 필드는 채점에서 빠집니다.
- 표기까지 정확히 같아야 맞은 것으로 셉니다. 예를 들어 `0723`과 `07:23`은 다른 값입니다. 표기 규칙은 스키마의 `description`에 적어 두세요.
- `data/`, `results/`, `.env`는 git에 올라가지 않습니다.

### 3. 정확도 측정

```bash
./run.sh evaluate                                       # Sonnet 5.5, Haiku 5.5, Textract+LLM
./run.sh evaluate --engines sonnet opus textract-analyze bda
./run.sh evaluate --only scan_01.png --details          # 틀린 필드를 화면에 출력
./run.sh evaluate --engines sonnet gpt-6.1-terra --repeat 3   # 같은 문서를 3번씩 측정
```

선택할 수 있는 엔진은 `opus` `sonnet` `haiku` `gpt-6.1-sol` `gpt-6.1-terra` `gpt-6-astra` `gpt-6-sol` `gpt-6-luna` `textract-detect` `textract-analyze` `bda`입니다. GPT 모델은 이미지만 받으므로 PDF는 PNG·JPEG로 바꿔서 넣어 주세요.

결과는 `results/eval_<시각>/` 폴더에 저장됩니다.

| 파일 | 내용 |
|---|---|
| `summary.csv` | 엔진별 평균 정확도·시간·토큰·비용 |
| `per_document.csv` | 문서 × 엔진 × 회차별 결과. 입력·캐시·출력 토큰과 비용을 호출마다 기록 |
| `mismatches.csv` | 정답과 다른 필드 목록 |

### 4. 화면에서 보기와 사람 검수

```bash
./run.sh            # Windows: .\run.ps1
```

http://127.0.0.1:7860 을 엽니다.

- 문서를 고르고 엔진을 선택해 처리하면 엔진별 결과와 정확도가 나옵니다.
- **Review** 탭에서는 사람이 확인해야 할 필드만 따로 모아 보여줍니다. 아래 중 하나라도 해당하면 목록에 올라갑니다.
  - 검산 규칙 위반
  - 엔진 간 값 불일치
  - Textract 신뢰도 낮음
  - 한글 값 (Textract로 교차 확인할 수 없음)
- 확정 값을 고쳐 저장하면 `results/reviewed/`에 수정 이력과 함께 남습니다.

## AI 코딩 도구로 설치하기 (Kiro · Claude Code · Codex)

이 저장소를 연 상태에서 아래처럼 요청하면 됩니다.

> "ocr-poc-setup 진행해줘. 프로필은 `my-poc`, 리전은 `ap-northeast-2`."

AI 도구는 다음 순서로 진행합니다.

1. 계정 확인
2. 설치·점검
3. (요청 시) 버킷 생성 승인 받기
4. 스키마 초안 작성
5. 정확도 측정과 결과 해석

절차는 [`AGENTS.md`](AGENTS.md)에 있고, 각 도구는 다음 경로에서 이 절차를 읽습니다.

| 도구 | 읽는 위치 |
|---|---|
| Kiro | `AGENTS.md`, `/ocr-poc-setup` 스킬 (`.kiro/skills/`) |
| Claude Code | `CLAUDE.md` → `AGENTS.md`, `ocr-poc-setup` 스킬 (`.claude/skills/`) |
| Codex | `AGENTS.md` |

정답 값은 AI가 채우지 않도록 되어 있습니다. 모델 결과로 정답을 채우면 정확도 측정이 의미 없어지기 때문입니다.

## 설정 (`.env`)

`setup.sh`가 작성하며, 직접 수정해도 됩니다. 예시는 [`.env.example`](.env.example)에 있습니다. `.env`에 적힌 값이 셸 환경변수보다 우선합니다. 다른 계정 프로필이 셸에 남아 있어도 점검한 계정만 쓰도록 하기 위해서입니다.

| 키 | 설명 |
|---|---|
| `AWS_PROFILE`, `AWS_REGION` | 호출할 계정 프로필과 리전 |
| `OCR_MODEL_SCOPE` | Bedrock 교차 리전 프로필 접두어. `global`(기본, 전 세계 리전에서 처리될 수 있음) 또는 `us`·`eu`·`apac` 등 지역 한정. 해당 리전에 모델이 없으면 점검 단계에서 알려줍니다. |
| `OCR_S3_BUCKET`, `OCR_BDA_ENABLED` | PDF·BDA용 비공개 버킷, BDA 사용 가능 여부 |
| `OCR_DATA_DIR`, `OCR_RESULTS_DIR` | 데이터·결과 폴더 (기본 `data/`, `results/`. `data/images`가 비어 있으면 원본 `sample/` 사용) |

## 비용과 데이터 처리

비용은 Bedrock 응답에 담긴 토큰 수로 호출마다 계산합니다.

```
비용 = 입력 토큰 × 입력 단가 + 캐시 쓰기 토큰 × 캐시 쓰기 단가
     + 캐시 읽기 토큰 × 캐시 읽기 단가 + 출력 토큰 × 출력 단가  (+ Textract·BDA 페이지 요금)
```

- 단가는 [`shared/config.py`](shared/config.py)에 있습니다. 온디맨드 공개 단가(Global 교차 리전, 2026-10-27 확인)이며, 서울과 미국 동부 리전이 같습니다. `OCR_MODEL_SCOPE`가 `global`이 아니면 10% 높은 지역 단가로 계산합니다.
- GPT 모델은 같은 프롬프트 앞부분을 자동으로 캐시합니다. 같은 이미지를 반복해서 돌리면 캐시 할인이 적용돼 실제보다 싸게 보이므로, 캐시가 없을 때의 비용도 `cost_uncached_usd`에 함께 남깁니다.
- 세금, 할인, 사람 검수 인건비는 포함하지 않습니다. 실제 청구액은 AWS Billing에서 확인하세요.
- 참고로 제주항공 비행일지 샘플 1장(필드 83개)에서 측정한 값은 다음과 같습니다(2026-10-27, 3~8회 평균).

| 엔진 | 입력 / 출력 토큰 | 페이지당 비용 |
|---|---|---|
| Claude Sonnet 5.5 | 6,220 / 943 | $0.022 |
| Claude Opus 5.5 | 6,220 / 1,875 | $0.062 |
| Claude Haiku 5.5 | 6,220 / 3,942 | $0.003 |
| GPT-6.1 Terra | 4,693 / 1,786 | $0.015 |
| GPT-6 Astra | 4,684 / 2,578 | $0.187 |
| Textract 텍스트 추출 + Sonnet 5.5 | 5,028 / 3,301 | $0.045 |

  문서 양식과 뽑는 항목 수에 따라 출력 토큰이 달라지므로, 자사 문서로 다시 측정해 보세요.
- `OCR_MODEL_SCOPE=global`이면 Bedrock 요청이 다른 리전에서 처리될 수 있습니다. 사내 데이터 정책을 먼저 확인하세요.
- Textract와 BDA의 문서 처리는 한글을 공식 지원하지 않습니다. 한글 손글씨는 Claude·GPT 모델이 이미지를 직접 읽는 방식으로 처리합니다.
- 샘플 몇 장의 결과는 운영 정확도를 보장하지 않습니다. 필체와 스캔 품질이 다양한 문서로 확인하세요.
- 같은 문서도 실행할 때마다 결과가 조금 달라질 수 있습니다. 예를 들어 7과 2가 헷갈리는 글씨는 한 번은 맞고 다음엔 틀리기도 합니다. 정확도를 비교할 때는 문서 수를 늘리고 여러 번 돌려 보세요.

## 정리

```bash
rm -rf data/images/* data/truth/* results .env
aws s3 rb s3://<setup에서 만든 버킷> --force --profile <프로필>   # 버킷과 안의 파일 삭제 (되돌릴 수 없음)
```

## 폴더 구조

```
app.py / ui.py / event_handler.py    웹 화면 (Gradio)
evaluate.py                          일괄 정확도 측정
engines/                             Textract · Bedrock · BDA 호출
shared/review.py                     사람 검수 대상 선별 (검산 규칙, 엔진 비교, 신뢰도)
scripts/setup_env.py                 점검·버킷·.env 작성
setup.sh / setup.ps1 / run.sh / run.ps1
examples/                            스키마·정답 예시 (가상 데이터)
sample/                              원본 저장소의 영문 샘플
arch-template/                       (참고용) 원본 저장소의 서버리스 처리 템플릿 — 이 데모에서는 쓰지 않습니다
```

## License

MIT-0. [LICENSE](LICENSE) 참고.
