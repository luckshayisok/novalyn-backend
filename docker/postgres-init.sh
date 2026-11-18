#!/usr/bin/env bash
# Runs once, when the PostgreSQL container creates its data directory.
#
# The container's bootstrap user is a superuser, and superusers skip Row-Level Security.
# NOVALYN therefore connects as an ordinary role that owns its database and tables, so the
# tenant isolation policies apply to every query the application makes.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
CREATE ROLE novalyn_app LOGIN PASSWORD '${APP_DB_PASSWORD}' NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
CREATE DATABASE novalyn OWNER novalyn_app;
SQL
