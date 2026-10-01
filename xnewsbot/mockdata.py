"""レイアウト確認用のモック(サンプル)ニュース。

実際の収集/キュレーション(twitterapi.io / Claude)を走らせずに配信レイアウトを確認する
ためのダミーデータ。LINEで「テスト」等を送ると、現在の配信レイアウトでこのサンプルが
返る(冒頭に「架空」警告つき)。API も時間も使わずレイアウト調整を回せる。

- 内容はすべて架空(事実ではない)。配信時は WARNING を必ず先頭に付ける。
- DB にはセンチネル日付 MOCK_DATE で格納し、実配信(当日分)と絶対に衝突させない。
- 出典名も架空(実在の媒体名・アカウントは使わない)。市況 MOCK_MARKET の値も架空。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlmodel import Session

from . import digest
from .genres import display_genres
from .models import GenreDigest, NewsItem

# 実配信の当日分と衝突しないセンチネル(過去の固定日)
MOCK_DATE = date(2000, 1, 1)
MOCK_SLOT = "morning"

WARNING = (
    "⚠️ これはレイアウト確認用のサンプルです。\n"
    "実際のニュースではありません(内容はすべて架空)。"
)

# 架空の市況(要点バブルの市況ブロック確認用)。値はすべて架空。
MOCK_MARKET: list[dict] = [
    {"key": "nikkei", "label": "日経平均", "close": 45210.35, "change": 540.12, "change_pct": 1.21,
     "asof": MOCK_DATE.isoformat(), "kind": "index"},
    {"key": "sp500", "label": "S&P500", "close": 6512.4, "change": -34.71, "change_pct": -0.53,
     "asof": MOCK_DATE.isoformat(), "kind": "index"},
    {"key": "usdjpy", "label": "ドル円", "close": 149.5, "change": 0.0, "change_pct": 0.0,
     "asof": MOCK_DATE.isoformat(), "kind": "fx"},
    {"key": "us10y", "label": "米10年債", "close": 4.25, "change": 0.03, "change_pct": 0.71,
     "asof": MOCK_DATE.isoformat(), "kind": "yield"},
    {"key": "btc", "label": "ビットコイン", "close": 118234.7, "change": 2668.1, "change_pct": 2.31,
     "asof": MOCK_DATE.isoformat(), "kind": "crypto"},
    {"key": "eth", "label": "イーサリアム", "close": 4321.8, "change": -52.4, "change_pct": -1.2,
     "asof": MOCK_DATE.isoformat(), "kind": "crypto"},
]

# 架空の twitterapi.io クレジット消費(要点バブルの「X取得」行の確認用)。値は架空。
MOCK_X_USAGE: dict = {"used": 9870, "remaining": 3_040_677}

# 架空の LINE 無料枠の使用状況(要点バブルの「LINE 今月」行の確認用)。値は架空。
MOCK_LINE_QUOTA: dict = {"limit": 200, "used": 45, "cost": 3}

# 架空の「今日の予定」(要点バブルの予定ブロック確認用)。数値・企業名はすべて架空。
# at は入れない: モックの日付は過去の固定日(MOCK_DATE)なので、時刻を入れると「過ぎた予定」として
# 全部消えてしまう。at が無い予定はこの並びのまま出る。
MOCK_SCHEDULE: list[dict] = [
    {"at": None, "time_label": "寄り前", "kind": "earnings", "country": "JP",
     "name": "サンプル商事 決算（4〜9月期）", "forecast": "", "previous": "", "result": "",
     "importance": 3},
    {"at": None, "time_label": "08:50", "kind": "indicator", "country": "JP",
     "name": "日 機械受注（前月比）", "forecast": "1.2%", "previous": "-0.5%", "result": "",
     "importance": 3},
    {"at": None, "time_label": "引け後", "kind": "earnings", "country": "JP",
     "name": "テスト自動車 決算（4〜9月期）", "forecast": "", "previous": "", "result": "",
     "importance": 4},
    {"at": None, "time_label": "21:30", "kind": "indicator", "country": "US",
     "name": "米 雇用統計（非農業部門雇用者数）", "forecast": "12.0万人", "previous": "14.2万人",
     "result": "", "importance": 5},
    {"at": None, "time_label": "23:00", "kind": "speech", "country": "US",
     "name": "米 FRB議長 講演", "forecast": "", "previous": "", "result": "", "importance": 4},
    {"at": None, "time_label": "翌03:00", "kind": "policy", "country": "US",
     "name": "米 FOMC 政策金利発表", "forecast": "4.25%", "previous": "4.50%", "result": "",
     "importance": 5},
    {"at": None, "time_label": "未定", "kind": "earnings", "country": "US",
     "name": "Sample Cloud 決算（7〜9月期）", "forecast": "", "previous": "", "result": "",
     "importance": 3},
]

# 架空の出典 (表示名, 種別)。大ニュースは3件・小ニュースは1件を順に割り当てる。
_MOCK_SOURCES: list[tuple[str, str]] = [
    ("サンプル経済新聞", "news"), ("@sample_news", "x"), ("Sample Wire", "news"),
    ("@demo_tech", "x"), ("テスト通信", "news"),
]

# (genre, importance, score, title, summary, detail) —
#   summary=一覧/inline用の簡潔要約、detail=「詳細を見る」で出す長め解説。すべて架空。
_MOCK: list[tuple[str, str, int, str, str, str]] = [
    ("特大", "big", 95, "首都圏で大規模停電、約200万世帯に影響 復旧を急ぐ",
     "送電設備のトラブルが原因とみられ、鉄道や信号にも影響が出ている。電力会社は数時間以内の復旧を見込むと発表した。",
     "午後3時ごろ、変電所の設備故障をきっかけに東京・神奈川・埼玉の広い範囲で停電が発生した。電力会社によると影響は約200万世帯にのぼり、一部の鉄道は運転を見合わせ、主要交差点では信号が消灯して警察官による手信号が行われている。病院など重要施設は非常用電源で対応中。復旧は数時間以内を見込むとしているが、原因設備の特定を急いでおり、再発防止策もあわせて調査している。気象や需給の急変が原因ではないとみられる。"),
    ("AI", "big", 88, "OpenAIが新モデル「GPT-X」を発表 推論速度が従来比2倍に",
     "長文処理とコスト効率を大幅に改善したとされる。開発者向けAPIを今週から段階的に提供する。",
     "OpenAIは新しい大規模言語モデル「GPT-X」を発表した。発表によると推論速度は従来比でおよそ2倍、長文コンテキストの取り回しとコスト効率も大きく改善したという。開発者向けAPIは今週から段階的に提供を開始し、価格は既存モデルより引き下げられる見込み。エージェント用途やコード生成での性能向上を強調しており、競合各社の動向にも影響しそうだ。安全性評価の結果も同時に公開された。"),
    ("AI", "small", 55, "国産LLM、医療分野での実証実験を開始",
     "国内の研究機関と病院が連携し、診療記録の要約支援で国産LLMの実証を始めた。",
     "国内の研究機関と複数の病院が連携し、国産の大規模言語モデルを使った診療支援の実証実験を開始した。当面は電子カルテの要約や問診メモの整理など、医師の事務負担を減らす用途が中心。個人情報を院内に閉じて処理する構成を採り、精度と安全性を数か月かけて検証する。将来的には専門科向けの知識補助への展開も視野に入れるという。"),
    ("AI", "small", 60, "EUのAI規制法、最終調整の段階に",
     "汎用AIの透明性義務やリスク分類をめぐり、EUが規則の細部を詰めている。",
     "EUのAI規制法(AI Act)が施行に向けた最終調整の段階に入った。汎用AIモデルに対する透明性義務や、用途ごとのリスク分類の線引きが主な論点。違反時の制裁金の水準や、研究・オープンソースの扱いについても各国・業界から意見が出ている。段階的な適用スケジュールが示される見通しで、域外企業にも影響が及ぶとみられる。"),
    ("株", "big", 85, "日経平均、史上最高値を更新し4万円台後半に",
     "半導体関連と輸出株が指数を牽引した。海外勢の買いが続いているとの見方が強い。",
     "東京株式市場で日経平均株価が史上最高値を更新し、終値で4万円台後半をつけた。半導体関連と輸出株が指数を牽引し、円安基調も追い風となった。海外投資家の買いが続いているとの見方が強く、出来高も膨らんだ。一方で過熱感を指摘する声もあり、高値圏での値動きは荒くなりやすいとの慎重論も出ている。今後は決算と為替の動向が焦点。"),
    ("株", "small", 58, "半導体株が軒並み上昇、関連ETFにも資金流入",
     "AI向け需要への期待から半導体関連が買われ、関連ETFにも資金が入った。",
     "AI向けの旺盛な需要への期待を背景に、半導体関連株が軒並み上昇した。主力銘柄の上昇に連動して関連ETFにも資金が流入し、セクター全体が物色された。設備投資の拡大観測や受注の好調さが買い材料。ただし株価の水準は既に高く、業績の裏付けを欠けば調整も入りやすいとの指摘もある。"),
    ("株", "small", 45, "新NISA、口座開設数が前年比1.5倍に",
     "制度拡充を受け、若年層を中心に新NISAの口座開設が大きく伸びている。",
     "新NISAの口座開設数が前年比でおよそ1.5倍に増えた。非課税枠の拡大と制度の恒久化を受け、これまで投資をしてこなかった若年層の参加が目立つ。積立投資による分散が中心で、インデックス型の投信に資金が集まりやすい。金融機関は手数料の引き下げや教育コンテンツの拡充で顧客獲得を競っている。"),
    ("テクノロジー", "big", 80, "国産ヒューマノイドロボット、工場向けに量産開始へ",
     "人手不足が続く製造現場向けに、国内メーカーがヒューマノイドロボットの量産を発表した。来年から順次出荷される。",
     "国内メーカーが、工場の搬送・組立作業を担うヒューマノイドロボットの量産開始を発表した。人手不足が深刻な製造現場向けに、まず数百台規模で来年から順次出荷する。カメラと基盤モデルを組み合わせた動作学習により、ライン変更への追従が従来の産業用ロボットより速いという。価格はリースで月額数十万円からとし、中堅工場でも導入しやすい水準を狙う。海外勢との競争が激しい分野で、国産勢の量産化は初となる。"),
    ("テクノロジー", "small", 52, "次世代EV電池、航続距離1.5倍の新型を発表",
     "国内電池メーカーがエネルギー密度を高めた新型EV電池を発表し、量産ラインの建設を始めた。",
     "国内の電池メーカーが、エネルギー密度を従来比で大きく高めた次世代EV電池を発表した。搭載車の航続距離は約1.5倍になる見込みで、急速充電への耐性も改善したという。量産ラインの建設をすでに始めており、数年内の車載採用を目指す。EVの普及を左右する電池性能の競争は世界的に激化しており、国内勢の巻き返しにつながるかが注目される。"),
    ("AI", "small", 62, "AIコーディング支援、大手SIerが全社標準に採用",
     "大手SIerが開発現場全体にAIコーディング支援ツールを標準導入し、生産性の計測結果も公開した。",
     "大手SIerが、AIコーディング支援ツールを全社の開発標準として採用したと発表した。数千人規模のエンジニアが日常的にコード生成・レビュー支援を使う体制となり、試験導入では実装工数が2〜3割減ったとの計測結果も公開した。セキュリティ面では社内コードの学習利用を制限する契約を結び、機密案件では利用範囲を絞る。国内大手での全社採用は導入判断の目安になるとして、他社の追随が見込まれる。"),
    ("暗号資産", "big", 78, "イーサリアム、大型アップグレードを来月実施へ",
     "手数料の引き下げと処理能力の向上を狙う大型アップグレードの実施日が決まった。対応を表明する取引所も相次いでいる。",
     "イーサリアムの開発者会議で、大型アップグレードを来月実施することが正式に決まった。取引手数料の引き下げと1秒あたりの処理件数の向上が主な狙いで、テストネットでの検証は大きな問題なく完了したという。国内外の主要取引所は、実施前後に入出金を一時停止して対応すると相次いで表明した。過去のアップグレードでは前後に価格が大きく動いた例もあり、市場の関心は高い。"),
    ("暗号資産", "small", 50, "ビットコイン、最高値圏で推移",
     "現物ETFへの資金流入を背景に、ビットコインは最高値圏での推移が続いている。",
     "ビットコインが最高値圏での推移を続けている。現物ETFを通じた資金流入が支えとなり、機関投資家の関心も高い。半減期後の需給や金利環境が今後の値動きを左右するとみられる。一方でボラティリティは依然として大きく、急騰の反動安にも警戒が必要との見方が出ている。"),
    ("話題", "big", 75, "人気ゲームの続編発表、予約開始直後に販売サイトが混雑",
     "シリーズ最新作の発売日と価格が発表され、予約開始から数分で販売サイトにアクセスが集中した。",
     "人気ゲームシリーズの最新作が発表され、発売日と価格が明らかになった。予約受付の開始直後から販売サイトにアクセスが集中し、一時つながりにくい状態が続いた。前作は世界で累計1000万本を超えており、今作は対応機種を広げて海外同時発売とする。開発元は追加の予約枠を順次用意するとしている。"),
    ("話題", "small", 48, "週末の流星群、各地で観測しやすい見込み",
     "天候に恵まれる地域が多く、週末の夜は流星群を観測しやすい見込み。月明かりの影響も小さいという。",
     ""),
    # 要約が100字を超える例(一覧で「…」に切り詰められる表示の確認用)
    ("話題", "small", 42, "駅ナカの無人書店、試験導入から半年で利用者が倍に",
     "駅構内に置いた無人書店の利用者が、試験導入から半年でおよそ2倍になった。通勤客の短時間の立ち寄りが多く、電子決済だけで買える手軽さが受けているという。運営会社は来年度中に設置駅を首都圏の主要駅へ広げる計画だ。",
     ""),
]


def _mock_genres() -> list[str]:
    """モックに含まれるジャンルを表示順(display_genres)で返す。"""
    seen = list(dict.fromkeys(g for g, *_ in _MOCK))  # 出現順で重複排除
    return display_genres(seen)


def seed(session: Session) -> int:
    """モックを DB に投入する(再実行で冪等: 同センチネルの既存を置き換える)。投入件数を返す。"""
    by_genre: dict[str, list[tuple[str, str, int, str, str, str]]] = {}
    for row in _MOCK:
        by_genre.setdefault(row[0], []).append(row)

    now = datetime.now(UTC)  # 「N時間前」が自然に見えるよう、投入時刻から少しずつ遡らせる
    n = 0
    for genre, rows in by_genre.items():
        existing = digest.get_genre_digest(session, genre, MOCK_DATE, MOCK_SLOT)
        if existing:
            for it in digest.items_of_digest(session, existing.id):
                session.delete(it)
            session.delete(existing)
            session.commit()
        gd = GenreDigest(digest_date=MOCK_DATE, slot=MOCK_SLOT, genre=genre)
        session.add(gd)
        session.commit()
        session.refresh(gd)
        for rank, (_g, importance, score, title, summary, detail) in enumerate(rows):
            views = 90000 - n * 500
            tweets = []
            for k in range(3 if importance == "big" else 1):
                media, kind = _MOCK_SOURCES[(n + k) % len(_MOCK_SOURCES)]
                url = (f"https://x.com/i/web/status/90000000{n:02d}{k}" if kind == "x"
                       else f"https://example.com/news/{n:02d}{k}")
                created = now - timedelta(minutes=20 + n * 37 + k * 15)
                # 出典本文は見出しの再掲ではなく、詳細解説相当の本文を入れる(詳細表示の確認用)
                tweets.append({"text": detail or summary or title, "author": media.lstrip("@"),
                               "url": url, "views": views if kind == "x" else 0,
                               "media": media, "created_at": created.isoformat(), "kind": kind})
            session.add(NewsItem(
                genre_digest_id=gd.id, genre=genre, importance=importance, rank=rank,
                title=title, summary=summary, detail=detail,
                source_urls=[t["url"] for t in tweets], source_tweets=tweets,
                top_view_count=views, score=score,
            ))
            n += 1
        session.commit()
    return n


def assemble(session: Session) -> dict[str, list[NewsItem]]:
    """モックを表示順で組み立てて返す(無ければ空)。"""
    return digest.assemble_for_genres(session, _mock_genres(), MOCK_DATE, MOCK_SLOT)


def get_or_seed(session: Session) -> dict[str, list[NewsItem]]:
    """モックを投入し直してから組み立てて返す(初回でも必ず出る)。

    毎回入れ直すのは、出典の「N時間前」を投入時刻基準で自然に見せるためと、
    古い形式で投入済みのモック(出典名・score なし)を残さず現行レイアウトで見せるため。"""
    seed(session)
    return assemble(session)
