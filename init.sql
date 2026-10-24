CREATE TABLE topics (
    id    SERIAL PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE questions (
    id          SERIAL PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    text        TEXT NOT NULL,
    priority    SMALLINT NOT NULL DEFAULT 2,  
    key_points  TEXT,                        
    UNIQUE (topic_id, text)
);

CREATE TABLE users (
    telegram_id  BIGINT PRIMARY KEY,
    xp           INTEGER NOT NULL DEFAULT 0,
    streak       INTEGER NOT NULL DEFAULT 0,
    last_active  DATE,
    daily_goal     SMALLINT,                   
    notify_time    TIME, 
    last_notified  DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE user_progress (
    user_id        BIGINT NOT NULL REFERENCES users(telegram_id),
    question_id    INTEGER NOT NULL REFERENCES questions(id),
    confidence     SMALLINT NOT NULL, 
    interval_days  INTEGER NOT NULL,
    next_review_at TIMESTAMPTZ NOT NULL,
    attempts_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, question_id)
);

CREATE TABLE attempts (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users(telegram_id),
    question_id  INTEGER NOT NULL REFERENCES questions(id),
    answer_text  TEXT NOT NULL,
    ai_feedback  JSONB,                  
    score        SMALLINT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);