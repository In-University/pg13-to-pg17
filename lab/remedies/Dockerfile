# postgres:<major> + pg_hint_plan + contrib (pg_stat_statements, auto_explain đã có sẵn trong image chuẩn)
ARG PG_MAJOR=17
FROM postgres:${PG_MAJOR}
ARG PG_MAJOR
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-pg-hint-plan \
 && rm -rf /var/lib/apt/lists/*
