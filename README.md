# school-notice-bot (가톨릭대 공지 알림봇)

성심교정 공지·기숙사·학과 게시판 8곳을 모아 **"기한 내에 무언가 해야 하는 공지"만** 골라 마감일 기준으로 알린다.

기숙사 공지는 `dorm.catholic.ac.kr`의 별도 게시판 4개로 쪼개져 있고, 본교 공지에는 일부만 재게시된다(→ [docs/M1_RESULT.md](docs/M1_RESULT.md) §5). 본교 공지만 봐서는 입사 모집 공고를 놓치기 쉬운 구조라서 만들어졌다.

**이 봇의 목적은 공지를 모아 보여주는 게 아니라 마감일을 놓치지 않게 하는 것이다.** 전부 밀어넣으면 알림 피로로 뮤트된다.

## 상태

| 마일스톤 | 상태 |
|---|---|
| M1 파싱 검증 | ✅ 파서 PASS / ⚠️ 콘텐츠 이슈 → [docs/M1_RESULT.md](docs/M1_RESULT.md) |
| M2 추출 검증 | 🔨 정확도 기준 충족(94%/100%), **검수 18/30건** → [docs/M2_RESULT.md](docs/M2_RESULT.md) |
| M3 알림 | ✅ 실발송 검증 (알림·다이제스트·미판정 3종 채널 수신 확인) |
| M4 운영 | 🔨 워크플로·문서 완료, 배포 대기 → [docs/OPERATIONS.md](docs/OPERATIONS.md) |

**M1 핵심 발견:** 학교가 공지를 PNG 스크린샷으로 올려서 본문 텍스트 확보율이 34%에 그친다. 기숙사 입퇴사공지는 0~25%다. 이미지 판독으로 대응한다.

## 구조

```
cuk_bot/
  config.py     게시판 8개 정의, HTTP 예의 설정, 임계치
  fetcher.py    전역 1.5초 간격 강제 (병렬 요청 불가 구조)
  parser.py     목록·상세 파서 (div.b-content-box 확정)
  content.py    본문 → PDF → 스크린샷 → 제목 순 에스컬레이션
  extractor.py  Gemini 호출, 마감일 구조화 추출
  prompt.py     프롬프트·요청 설정 (모델별 thinking 적응)
  quota.py      무료 한도 예산 관리, 429 분류(일일 vs 분당)
  judge.py      모델 체인 — 한도 소진 시 다음 모델로 승계
  client.py     운영(AI Studio) / 테스트(Vertex) 자격증명 분리
  health.py     하트비트 ping, 게시판 연속 실패 감지
  status.py     수집 상태·한도·토큰 비용 리포트
  collector.py  게시판 순회, 새 글만 상세 진입
  notifier.py   텔레그램 발송, D-day, 리마인더 예약
  cli.py        커맨드
docs/           핸드오프 명세, M1 판정 결과
tools/          M1·M2 검증 하네스
tests/          리마인더·한도·모델체인·파싱 단위 테스트
reference/      원본 스켈레톤 (실행 금지, 참고용)
```

## 설치

```bash
pip install requests beautifulsoup4 google-genai pillow pymupdf
```

## 환경변수

| 이름 | 필수 | 기본값 | 용도 |
|---|---|---|---|
| `GEMINI_API_KEY` | ✅ | — | LLM 추출 (**AI Studio 키**, 무료 한도) |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | @BotFather 발급 |
| `TELEGRAM_CHAT_ID` | ✅ | — | 채널 chat id (채널은 `-100…` 형태) |
| `CUK_DB` | | `cuk_notices.db` | SQLite 경로 |
| `CUK_MODEL` | | `gemini-3.1-flash-lite` | 1순위 모델 |
| `CUK_MODEL_CHAIN` | | 3.1→3.5→3.6→2.5→alias | 한도 소진 시 넘어갈 모델 순서 |
| `CUK_VERTEX_CREDENTIALS` | | — | **테스트 전용** 서비스 계정 JSON 경로 (설정 시 Vertex, 과금) |
| `CUK_VERTEX_PROJECT` | | JSON의 project_id | Vertex 프로젝트 override |
| `CUK_VERTEX_LOCATION` | | `global` | Vertex 리전 |
| `CUK_GEMINI_RPD` | | `180` | 일일 상한 초기 추정값 (API가 알려주면 교체됨) |
| `CUK_GEMINI_RPM` | | `3` | 분당 상한 (로컬 가드) |
| `CUK_NOTIFY_UNJUDGED` | | `1` | `0`이면 판정 불가 시 알림도 안 함 |
| `CUK_CACHE` | | `.cache` | 다운로드 캐시 |
| `CUK_CONTACT_EMAIL` | | — | User-Agent에 남길 연락처 |
| `CUK_MAX_IMAGES` | | `3` | 공지당 판독할 이미지 수 |
| `CUK_READ_IMAGES` | | `1` | `0`이면 이미지 판독 끄고 제목만 사용 |
| `CUK_HEALTHCHECK_URL` | | — | `--check` 용 healthchecks.io ping URL |
| `CUK_HEALTHCHECK_DIGEST_URL` | | — | `--digest` 용 (별도 모니터) |

### API 키 주의

무료 한도는 **AI Studio에서 발급한 Developer API 키**(`AIza…`)에서만 나온다. GCP 서비스 계정 JSON은 Vertex AI로 붙어 **과금 대상**이므로 이 봇에 쓰면 안 된다. 발급: [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

## 무료 한도 소진 시 동작 (fail-open)

**판정할 수 없으면 필터링을 멈출 뿐, 알림은 멈추지 않는다.**

- 한도 소진 · 키 누락 · API 장애 · 응답 파싱 실패 — 어떤 이유든 판정 불가면 해당 공지를 **판정 없이 전달**한다
- 실행 1회당 미판정 건을 **하나의 메시지로 묶어** 발송한다. 20건이 밀려도 알림은 1개다
- 미판정 공지는 추출 레코드를 남기지 않으므로, 한도 회복 후 `--reextract`가 자동으로 다시 판정하고 D-7/3/1 리마인더를 그때 예약한다

### 측정된 무료 한도 (2026-08-05)

| 모델 | 일일 한도 | 근거 |
|---|---|---|
| `gemini-2.5-flash` | **20건** | 429 응답의 `quotaValue` |
| `gemini-2.5-flash-lite` | **20건** | 429 응답의 `quotaValue` |
| 그 외 | 미관측 | 첫 429에서 자동 학습 |

하루 20~40건 올라오는 공지에 비해 모델 하나로는 턱없이 부족하다. **무료 한도는 모델별로 따로 잡히므로** 한 모델이 소진되면 다음 모델로 넘어간다(`CUK_MODEL_CHAIN`). 체인 전체가 소진돼야 판정을 포기한다.

한도 판정은 두 겹이다. 로컬 카운터는 API를 두드리지 않기 위한 사전 차단이고, **실제 기준은 API가 돌려주는 429**다. 429의 `quotaValue`를 모델별로 저장해서 이후에는 추정값 대신 관측값을 쓴다.

주의: 429의 `retryDelay`는 신뢰하면 안 된다. **일일 한도 소진인데도 "38초 후 재시도"를 준다.** 판정 근거는 `quotaId`의 `PerDay`/`PerMinute`다.

## 텔레그램 채널 연결

채널로 보내려면 봇이 **채널 관리자**여야 하고 "메시지 게시" 권한이 있어야 한다. 구독자로만 추가하면 아무 이벤트도 받지 못한다.

공개 채널이면 `getUpdates` 없이 바로 id를 얻을 수 있다.

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getChat?chat_id=@채널이름"
```

비공개 채널이면 봇을 관리자로 추가한 **뒤에** 메시지를 하나 올리고 `getUpdates` 의 `channel_post.chat.id` 를 쓴다. 봇 추가 이전 메시지는 잡히지 않는다.

PowerShell 에서는 `curl` 이 `Invoke-WebRequest` 별칭이라 `-s` 가 통하지 않는다. `curl.exe` 로 부르거나 `Invoke-RestMethod` 를 쓴다.

## 모델 수명 (중요)

**`gemini-2.5-*` 와 `gemini-2.0-*` 는 2026-10-16 종료 예정이다.** 그래서 체인 1순위를 `gemini-3.1-flash-lite` 로 두었고, 종료된 모델 이름은 `ModelUnavailable` 로 걸러 **다음 모델로 넘어갈 뿐 봇이 멈추지 않는다**. 체인 끝의 `gemini-flash-lite-latest` 별칭은 항상 현행 모델을 가리키므로 체인이 통째로 비는 상황을 막는다.

Gemini 3.x 계열은 `thinking_budget=0` 을 거부한다. 모델별로 첫 호출에서 감지해 thinking 을 켜고 출력 예산을 4096 으로 올린다(2.5 는 끄고 1024). 하드코딩이 아니라 런타임 학습이라 새 모델 이름이 추가돼도 코드 수정이 필요 없다.

## 운영 키 vs 테스트 키

| 용도 | 자격증명 | 경로 | 비용 |
|---|---|---|---|
| 운영 | `GEMINI_API_KEY` (AI Studio) | Gemini Developer API | 무료 한도 |
| 테스트 | `CUK_VERTEX_CREDENTIALS` (서비스 계정 JSON) | Vertex AI | **과금** |

`CUK_VERTEX_CREDENTIALS` 가 설정돼 있으면 그쪽이 우선한다. 모든 실행이 시작 시 `자격증명: ...` 을 출력하므로 어느 쪽으로 돌고 있는지 항상 보인다. 운영 cron 에는 이 변수를 **설정하지 않는다**.

## 사용

```bash
python -m cuk_bot --backfill    # 최초 1회 필수 — 안 하면 첫 가동에 알림 폭탄
python -m cuk_bot --check       # 새 글 수집 + 추출 + 즉시 알림
python -m cuk_bot --digest      # 다이제스트 + D-7/3/1 리마인더
python -m cuk_bot --dry-run     # 파싱 결과만 (LLM·텔레그램 미사용)
python -m cuk_bot --reextract   # 저장 본문으로 재추출 (프롬프트 튜닝용)
python -m cuk_bot --status      # 수집 상태·한도·예상 비용
python -m cuk_bot --renormalize # 판정 규칙 재적용 (API 미사용)
```

`--no-notify`를 붙이면 발송 없이 판정만 출력한다.

### 검증 도구

```bash
python tools/m1_probe.py                    # 파싱 가정 A1~A4 재판정
python tools/body_survey.py --per-board 8   # 본문 확보율 측정
python tools/m2_review.py --offline         # 콘텐츠 해석만 (API 미사용)
python tools/m2_review.py --limit 4         # 추출 표본 검수
python -m unittest discover -s tests        # 단위 테스트
```

## 운영

GitHub Actions 로 돌린다 — `.github/workflows/`. 24시간 30분 간격 수집, 매일 08:00 다이제스트. **Vercel 무료 티어는 불가**(cron 하루 1회 + 파일시스템 휘발). 배포·장애 대응은 [docs/OPERATIONS.md](docs/OPERATIONS.md).

침묵이 정상과 구분되지 않는 것이 이 봇의 가장 위험한 고장이라, 감지를 두 겹으로 뒀다 — 외부 감시(healthchecks.io)가 "죽었는지"를, `crawl_log` 가 "왜 죽었는지"를 맡는다. 새 공지가 없는 날에도 아침 메시지를 보내 **부재 자체가 신호**가 되게 했다.

## 주의

- 수집 대상은 **로그인 없이 공개된 공지사항**에 한정한다. 로그인·CAPTCHA 우회 금지
- `/_attach/` 이미지 판독은 robots.txt Disallow 경로에 대한 **의뢰인 승인 예외**다 (2026-08-03). 본문이 빈 공지에 한해, 공지당 3장까지, 캐시해서 1회만 받는다. 상세는 [docs/M1_RESULT.md](docs/M1_RESULT.md) §3
- 개인용·비공개 사용에 한정한다. 재배포·서비스화는 별도 검토 대상
- 학교에서 차단하거나 문의가 오면 즉시 중단하고 의뢰인에게 알린다
