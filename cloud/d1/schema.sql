-- 本番 SQLite (xnewsbot.db) の .schema をそのまま D1 に作る（2026-11-19 書き出し）
CREATE TABLE subscriber (
	id INTEGER NOT NULL, 
	line_user_id VARCHAR NOT NULL, 
	display_name VARCHAR, 
	enabled_genres JSON, 
	pending_genres JSON, 
	morning_hour INTEGER NOT NULL, 
	morning_minute INTEGER NOT NULL, 
	evening_hour INTEGER NOT NULL, 
	evening_minute INTEGER NOT NULL, 
	morning_enabled BOOLEAN NOT NULL, 
	evening_enabled BOOLEAN NOT NULL, 
	tz VARCHAR NOT NULL, 
	onboarding_step VARCHAR NOT NULL, 
	is_onboarded BOOLEAN NOT NULL, 
	last_morning_on DATE, 
	last_evening_on DATE, 
	created_at DATETIME NOT NULL, push_to VARCHAR, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_subscriber_line_user_id ON subscriber (line_user_id);
CREATE TABLE genredigest (
	id INTEGER NOT NULL, 
	digest_date DATE NOT NULL, 
	slot VARCHAR NOT NULL, 
	genre VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_genredigest_slot ON genredigest (slot);
CREATE INDEX ix_genredigest_digest_date ON genredigest (digest_date);
CREATE INDEX ix_genredigest_genre ON genredigest (genre);
CREATE TABLE newsitem (
	id INTEGER NOT NULL, 
	genre_digest_id INTEGER NOT NULL, 
	genre VARCHAR NOT NULL, 
	importance VARCHAR NOT NULL, 
	rank INTEGER NOT NULL, 
	title VARCHAR NOT NULL, 
	summary VARCHAR NOT NULL, 
	source_urls JSON, 
	source_tweets JSON, 
	top_view_count INTEGER NOT NULL, detail VARCHAR DEFAULT '', genres JSON DEFAULT '[]', score INTEGER DEFAULT 0, 
	PRIMARY KEY (id), 
	FOREIGN KEY(genre_digest_id) REFERENCES genredigest (id)
);
CREATE INDEX ix_newsitem_genre_digest_id ON newsitem (genre_digest_id);
CREATE TABLE breaking_sent(key TEXT PRIMARY KEY, title TEXT, url TEXT, genre TEXT, sent_at TEXT);
CREATE TABLE breaking_rejected(key TEXT PRIMARY KEY, title TEXT, reason TEXT, at TEXT);
CREATE TABLE marketsnapshot (
	id INTEGER NOT NULL, 
	digest_date DATE NOT NULL, 
	slot VARCHAR NOT NULL, 
	data JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_marketsnapshot_digest_date ON marketsnapshot (digest_date);
CREATE INDEX ix_marketsnapshot_slot ON marketsnapshot (slot);
CREATE TABLE schedulesnapshot (
	id INTEGER NOT NULL, 
	digest_date DATE NOT NULL, 
	slot VARCHAR NOT NULL, 
	data JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_schedulesnapshot_digest_date ON schedulesnapshot (digest_date);
CREATE INDEX ix_schedulesnapshot_slot ON schedulesnapshot (slot);
CREATE TABLE xusagesnapshot (
	id INTEGER NOT NULL, 
	digest_date DATE NOT NULL, 
	slot VARCHAR NOT NULL, 
	used INTEGER NOT NULL, 
	remaining INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_xusagesnapshot_digest_date ON xusagesnapshot (digest_date);
CREATE INDEX ix_xusagesnapshot_slot ON xusagesnapshot (slot);
CREATE TABLE newsexplanation (
	id INTEGER NOT NULL, 
	news_item_id INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	text VARCHAR NOT NULL, 
	error VARCHAR NOT NULL, 
	claim_token VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, title VARCHAR NOT NULL DEFAULT '', push_to VARCHAR NOT NULL DEFAULT '', push_cost INTEGER NOT NULL DEFAULT 0, 
	PRIMARY KEY (id), 
	FOREIGN KEY(news_item_id) REFERENCES newsitem (id)
);
CREATE UNIQUE INDEX ix_newsexplanation_news_item_id ON newsexplanation (news_item_id);
CREATE TABLE watchedaccount (
	id INTEGER NOT NULL, 
	handle VARCHAR NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_watchedaccount_handle ON watchedaccount (handle);
CREATE TABLE deliveryalert (
	"key" VARCHAR NOT NULL, 
	sent BOOLEAN NOT NULL, 
	PRIMARY KEY ("key")
);
CREATE TABLE newsfeedback (
	id INTEGER NOT NULL, 
	newsitem_id INTEGER NOT NULL, 
	user_id VARCHAR NOT NULL, 
	verdict VARCHAR NOT NULL, 
	genre VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	digest_date DATE, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_newsfeedback_newsitem_id ON newsfeedback (newsitem_id);
CREATE INDEX ix_newsfeedback_user_id ON newsfeedback (user_id);

-- クラウド移行で追加: DB 以外に持っていた状態(watch_state.json 等)と事前描画した spec を置く
CREATE TABLE IF NOT EXISTS kv (
	key VARCHAR NOT NULL PRIMARY KEY,
	value TEXT NOT NULL,
	updated_at VARCHAR NOT NULL
);
