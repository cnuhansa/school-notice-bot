# school-notice-bot (가톨릭대 공지 알림봇)

성심교정 공지·기숙사·학과 게시판 8곳을 모아 **"기한 내에 무언가 해야 하는 공지"만** 골라 마감일 기준으로 알린다.

기숙사 공지는 본교 공지사항에 전혀 올라오지 않고 `dorm.catholic.ac.kr`의 별도 게시판 4개로 쪼개져 있다. 본교 공지만 성실히 봐도 입사 모집 공고를 100% 놓치는 구조라서 만들어졌다.

**이 봇의 목적은 공지를 모아 보여주는 게 아니라 마감일을 놓치지 않게 하는 것이다.** 전부 밀어넣으면 알림 피로로 뮤트된다.

## 상태

| 마일스톤 | 상태 |
|---|---|
| M1 파싱 검증 | ✅ 파서 PASS / ⚠️ 콘텐츠 이슈 → [docs/M1_RESULT.md](docs/M1_RESULT.md) |
| M2 추출 검증 | 🔨 코드 완료, 표본 검수 미실행 (API 키 대기) |
| M3 알림 | 🔨 코드 완료, 실발송 미검증 (텔레그램 토큰 대기) |
| M4 운영 | ⬜ 미착수 |

**M1 핵심 발견:** 학교가 공지를 PNG 스크린샷으로 올려서 본문 텍스트 확보율이 34%에 그친다. 기숙사 입퇴사공지는 0~25%다. 이미지 판독으로 대응한다.

## 구조

```
cuk_bot/
  config.py     게시판 8개 정의, HTTP 예의 설정, 임계치
  fetcher.py    전역 1.5초 간격 강제 (병렬 요청 불가 구조)
  parser.py     목록·상세 파서 (div.b-content-box 확정)
  content.py    본문 → PDF → 스크린샷 → 제목 순 에스컬레이션
  extractor.py  Claude 호출, 마감일 구조화 추출
  collector.py  게시판 순회, 새 글만 상세 진입
  notifier.py   텔레그램 발송, D-day, 리마인더 예약
  cli.py        커맨드
docs/           핸드오프 명세, M1 판정 결과
tools/          M1·M2 검증 하네스
tests/          리마인더·이스케이프·파싱 단위 테스트
reference/      원본 스켈레톤 (실행 금지, 참고용)
```

## 설치

```bash
pip install requests beautifulsoup4 anthropic pillow pymupdf
```

## 환경변수

| 이름 | 필수 | 기본값 | 용도 |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | — | LLM 추출 |
| `TELEGRAM_BOT_TOKEN` | ✅ | — | @BotFather 발급 |
| `TELEGRAM_CHAT_ID` | ✅ | — | 수신자 chat id |
| `CUK_DB` | | `cuk_notices.db` | SQLite 경로 |
| `CUK_MODEL` | | `claude-sonnet-5` | 모델명 |
| `CUK_CACHE` | | `.cache` | 다운로드 캐시 |
| `CUK_CONTACT_EMAIL` | | — | User-Agent에 남길 연락처 |
| `CUK_MAX_IMAGES` | | `5` | 공지당 판독할 이미지 수 |
| `CUK_READ_IMAGES` | | `1` | `0`이면 이미지 판독 끄고 제목만 사용 |

## 사용

```bash
python -m cuk_bot --backfill    # 최초 1회 필수 — 안 하면 첫 가동에 알림 폭탄
python -m cuk_bot --check       # 새 글 수집 + 추출 + 즉시 알림
python -m cuk_bot --digest      # 다이제스트 + D-7/3/1 리마인더
python -m cuk_bot --dry-run     # 파싱 결과만 (LLM·텔레그램 미사용)
python -m cuk_bot --reextract   # 저장 본문으로 재추출 (프롬프트 튜닝용)
python -m cuk_bot --status      # 수집 상태와 파싱 실패 이력
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

## cron

```cron
*/10 9-19 * * 1-5  cd /path/to/bot && python -m cuk_bot --check  >> check.log 2>&1
0 8 * * *          cd /path/to/bot && python -m cuk_bot --digest >> digest.log 2>&1
```

평일 09~19시로 제한하는 건 학교 서버 배려다. 병렬 요청은 구조적으로 불가능하게 해뒀다.

## 주의

- 수집 대상은 **로그인 없이 공개된 공지사항**에 한정한다. 로그인·CAPTCHA 우회 금지
- `/_attach/` 이미지 판독은 robots.txt Disallow 경로에 대한 **의뢰인 승인 예외**다 (2026-08-03). 본문이 빈 공지에 한해, 공지당 5장까지, 캐시해서 1회만 받는다. 상세는 [docs/M1_RESULT.md](docs/M1_RESULT.md) §3
- 개인용·비공개 사용에 한정한다. 재배포·서비스화는 별도 검토 대상
- 학교에서 차단하거나 문의가 오면 즉시 중단하고 의뢰인에게 알린다
