"""Explicit translation migration. Dry-run by default; --apply creates a verified backup."""
import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from translation_review import migrate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    database = args.database.resolve()
    if not database.is_file():
        parser.error('数据库文件不存在。')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            parser.error('数据库完整性检查失败。')
        if conn.execute("SELECT 1 FROM grading_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            parser.error('仍有后台任务运行，请先停止服务并结束任务。')
        if not args.apply:
            print('仅检查：新增翻译细则与分析历史表，旧题保持 strict；未修改数据库。使用 --apply 执行。')
            return
        backup = database.with_name(database.name + '.translation-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        with closing(sqlite3.connect(backup)) as copied:
            conn.backup(copied)
            if copied.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('备份校验失败，未执行迁移。')
    migrate(database)
    with closing(sqlite3.connect(database)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('迁移校验失败，请保留现场并从备份恢复。')
    print(f'迁移与完整性检查通过。备份：{backup}')


if __name__ == '__main__':
    main()
