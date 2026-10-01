#!/bin/sh
# Полная сборка данных на сервере. Запуск: sh deploy/build.sh
set -e
cd /opt/tenderhack
export TH_DATA_DIR=/data/tenderhack
PY=.venv/bin/python

$PY -m app.etl.build_db
$PY -m app.search.index
$PY -m app.search.okpd_index
if [ -f $TH_DATA_DIR/ext/rmsp.zip ] && [ ! -f $TH_DATA_DIR/ext/rmsp.parquet ]; then
  $PY -m app.etl.rmsp
fi
echo BUILD_DONE
