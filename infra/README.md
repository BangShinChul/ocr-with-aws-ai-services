# 서버리스 OCR 파이프라인 (AWS CDK · Python)

데모 앱으로 정확도를 확인한 뒤, 같은 방식을 운영 형태로 돌려 보기 위한 스택입니다. S3에 스캔을 올리면 자동으로 판독하고, 검산에 걸린 칸만 사람에게 넘깁니다.

```
S3 incoming/<문서유형>/<파일>
  → EventBridge (Object Created) → SQS (+DLQ) → EventBridge Pipes
  → Step Functions
      ① 전처리 Lambda      형식·크기 확인, 문서 ID 발급
      ② Bedrock 판독 ‖ Textract 텍스트 추출 (병렬)
      ③ 검산 Lambda        합계·시간 규칙, Textract 근거·신뢰도 확인
      ├ 통과 → 결과 저장 (S3 results/, DynamoDB)
      └ 검수 필요 → 검수 대기 (DynamoDB, 작업 토큰) → 담당자 확정 → 결과 저장
  CloudWatch 대시보드·경보, 실패 시 상태 FAILED 기록
```

사람 검수에 Amazon Augmented AI(A2I)는 쓰지 않습니다. A2I는 신규 고객을 받지 않기 때문입니다. 대신 Step Functions가 작업 토큰을 받은 채 기다립니다. 담당자가 로컬 앱에서 값을 확정하면, 확정 값과 담당자 IAM ARN을 검수 테이블에 먼저 저장한 뒤 `SendTaskSuccess`로 처리를 이어 갑니다. 작업 토큰에는 `{"confirmed": true}`만 실어 보내므로, 이름·사번 같은 값은 Step Functions 실행 기록에 남지 않습니다.

## 노출 범위

| 리소스 | 외부 접근 | 접근 방식 |
|---|---|---|
| S3 버킷 2개 (문서, 접근 로그) | 없음. 퍼블릭 액세스 차단, HTTPS만 허용 | IAM |
| Lambda 7개 | 없음. Function URL·VPC·공인 IP 없음 | Step Functions만 호출 |
| Step Functions, SQS, Pipes, EventBridge | 없음 | 서비스 간 IAM 역할 |
| DynamoDB 테이블 2개 | 없음 | IAM |
| 검수 화면 | 이 PC의 127.0.0.1에서만 열림 | 로컬 AWS 자격 증명 |

API Gateway, CloudFront, 로드 밸런서, 보안 그룹, `0.0.0.0/0` 규칙은 만들지 않습니다. `cdk synth` 단계에서 cdk-nag(AwsSolutions) 검사를 통과해야 배포됩니다. 의도된 예외(접두어 단위 S3 권한, 리소스 지정이 불가능한 X-Ray·Textract 권한 등)는 [`ocr_pipeline/stack.py`](ocr_pipeline/stack.py)에 사유와 함께 적어 두었습니다.

## 준비

- Python 3.11 이상, Node.js 20 이상, AWS CLI v2
- 배포 권한이 있는 AWS 프로필 (PoC 전용 계정 권장)
- 배포할 리전에서 쓸 Bedrock 모델 사용 가능 (기본 `global.anthropic.claude-sonnet-5-5`)

Docker는 필요 없습니다. Lambda 코드는 `infra/lambda/handlers.py`와 `shared/review.py`, `shared/prompts.py`를 그대로 복사해 올립니다.

## 배포

```bash
cd infra
./deploy.sh --profile my-poc --region ap-northeast-2 --diff   # 만들어질 리소스만 확인
./deploy.sh --profile my-poc --region ap-northeast-2          # 계정 확인 → 검사 → 변경 내용 → 확인 후 배포
```

Windows에서는 `.\deploy.ps1 -AwsProfile my-poc -Region ap-northeast-2`를 씁니다.

설정은 `-c 키=값`으로 바꿉니다.

| 키 | 기본값 | 설명 |
|---|---|---|
| `modelId` | `global.anthropic.claude-sonnet-5-5` | 판독 모델. IAM 권한도 이 모델 하나로 한정됩니다 |
| `docTypePrompt` | `form` | 프롬프트 유형 (`generic`·`form`·`handwritten` 등) |
| `confidenceThreshold` | `90` | Textract 단어 신뢰도 기준 (%) |
| `reviewReasons` | `규칙 위반,저신뢰,OCR 근거 없음` | 검수로 넘길 사유. `엔진 간 불일치`, `한글 (OCR 교차검증 불가)`도 넣을 수 있습니다 |
| `reviewTtlDays` | `7` | 검수 대기 기한. 넘기면 해당 문서는 FAILED로 기록됩니다 |
| `readConcurrency` | `10` | 모델 판독 Lambda의 동시 실행 상한. 한꺼번에 많이 올려도 Bedrock 비용이 급증하지 않게 막습니다. 대기분은 Step Functions가 다시 시도합니다. `0`이면 상한 없음 |
| `retainData` | `false` | `true`면 스택을 지워도 버킷·테이블을 남깁니다 |
| `stackName` | `OcrPipeline` | 스택 이름. 바꾸면 `.env`의 `OCR_PIPELINE_STACK`도 같이 바꿔 주세요 |

## 사용

데모 앱과 같은 `.env`(프로필·리전)를 씁니다. 저장소 최상위 폴더에서 실행합니다.

```bash
./run.sh pipeline schema data/schema/flight-log.json --type flight-log   # 문서 유형별 스키마 (처음 한 번)
./run.sh pipeline upload data/images/scan_01.png --type flight-log       # 업로드하면 바로 처리 시작
./run.sh pipeline status                                                  # 처리 현황
./run.sh pipeline pending                                                 # 검수 대기 목록
./run.sh pipeline show <doc_id>                                           # 걸린 칸과 사유
./run.sh pipeline approve <doc_id> --set crew[0].name_kr=최중현            # 값 확정 후 처리 재개
./run.sh pipeline reject <doc_id> --reason "스캔 불량"
./run.sh pipeline result <doc_id>                                         # 최종 JSON
```

화면으로 하려면 `./run.sh`로 데모 앱을 열고 맨 아래 **서버리스 파이프라인** 영역을 펼칩니다. 원본 이미지를 보면서 `확정 값`을 고치고 승인하면 됩니다.

검수 담당자에게 필요한 권한은 다음 정도입니다: 결과·검수 테이블 `Query`·`GetItem`·`Scan`·`UpdateItem`, 문서 버킷 `GetObject`·`PutObject`, `states:SendTaskSuccess`·`SendTaskFailure`, `cloudformation:DescribeStacks`, `sts:GetCallerIdentity`.

결과 JSON에는 담당자가 입력한 이름과 호출한 IAM ARN이 함께 남습니다. 같은 문서를 두 사람이 동시에 처리하면 먼저 확정한 쪽만 반영됩니다.

## 데이터 처리

- **처리 리전**: 서울 리전의 Claude Sonnet 5.5는 `global.` 교차 리전 프로필로만 제공됩니다. 이 경우 문서 이미지가 서울 밖 리전에서 처리될 수 있으므로, 사내 데이터 정책을 먼저 확인해 주세요.
- **로그**: Lambda 로그에는 문서 ID·유형·크기만 남고, 파일명과 추출 값은 남기지 않습니다. Step Functions 로그에도 실행 데이터를 넣지 않습니다.
- **암호화**: S3는 SSE-S3, DynamoDB와 SQS는 AWS 관리 키로 암호화합니다. 운영에서는 보존 기간과 고객 관리형 KMS 키를 정해 주세요.
- **입력 검사**: `incoming/<문서유형>/<파일>` 형식만 받습니다. 문서 유형은 영문·숫자·`-`·`_`만 쓸 수 있고, PNG·JPEG·PDF만 처리합니다.

## 결과 위치

| 위치 | 내용 |
|---|---|
| `s3://<버킷>/results/<doc_id>.json` | 최종 값, 검수 여부·검수자·수정 이력, 모델 토큰 수 |
| DynamoDB 결과 테이블 | 문서별 상태 (`PROCESSING` → `COMPLETED`·`REVIEW_PENDING`·`REVIEWED`·`FAILED`) |
| DynamoDB 검수 테이블 | 검수 대기 항목 (기한이 지나면 TTL로 삭제) |
| CloudWatch 대시보드 `OcrPipeline-<리전>` | 처리 건수, 검수 비율, 토큰, 판독 시간, DLQ |

스캔(`incoming/`)은 30일, 중간 결과(`work/`)는 검수 기한 + 7일 뒤 지워집니다.

## 비용

문서가 없으면 거의 들지 않습니다. 페이지당 비용은 주로 Bedrock 판독(Sonnet 5.5 기준 약 $0.022)과 Textract 텍스트 추출($0.0015)이고, Step Functions·Lambda·S3·DynamoDB는 페이지당 약 $0.0004입니다(서울 리전 공개 단가 기준 추정). 실제 청구액은 AWS Billing에서 확인하세요.

## 삭제

```bash
./deploy.sh --profile my-poc --region ap-northeast-2 --destroy
```

`retainData=false`(기본)이면 버킷 안의 스캔·결과와 테이블까지 모두 지워집니다.

## 개발

```bash
cd infra
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q        # 핸들러 단위 테스트(AWS 호출 없음) + 템플릿 보안 검사
../.venv/bin/python -m pytest -q tests/test_pipeline_client.py   # 검수 클라이언트 (데모 앱 venv)
node_modules/.bin/cdk synth --quiet  # cdk-nag 포함
```
