"""管理Web UI(ニュース閲覧 + 設定管理)。LAN 限定・別ポート(8011)・別 launchd で常駐する。

既存の LINE Webhook サーバ(xnewsbot/api, :8010, ngrok 公開)とは完全に独立。APIキー管理を
含むため、このアプリは ngrok でトンネルせず 127.0.0.1/LAN 限定 + HTTP Basic 認証で運用する。
"""
