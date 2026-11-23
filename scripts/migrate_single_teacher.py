"""Copy one legacy workspace into a teacher-owned database; never edit the source."""
import argparse
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4


OWNED_TABLES = ('tasks', 'classes', 'submissions', 'assignments',
                'class_memberships', 'assignment_students', 'batch_photos', 'grading_jobs')


def migrate(source_data, accounts_db, target_data, teacher_email):
    source_data, accounts_db, target_data = map(Path, (source_data, accounts_db, target_data))
    source_db = source_data / 'dictation.sqlite3'
    if target_data.exists():
        raise FileExistsError(f'目标目录已存在：{target_data}')
    if not source_db.is_file() or not accounts_db.is_file():
        raise FileNotFoundError('原数据库或账号数据库不存在。')
    if target_data.resolve().is_relative_to(source_data.resolve()):
        raise ValueError('目标目录必须独立于原数据目录。')
    with closing(sqlite3.connect(accounts_db.resolve().as_uri() + '?mode=ro', uri=True)) as accounts:
        teacher = accounts.execute('''SELECT id,name,email,password_hash,role,disabled_at FROM users
            WHERE lower(email)=lower(?)''', (teacher_email,)).fetchone()
    if not teacher or teacher[4] != 'teacher' or teacher[5] is not None:
        raise ValueError('指定邮箱没有可用的教师账号。')
    with closing(sqlite3.connect(source_db.resolve().as_uri() + '?mode=ro', uri=True)) as source:
        if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('原数据库完整性检查未通过。')
        if source.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('原数据库存在无效关联。')
        if source.execute("SELECT 1 FROM grading_jobs WHERE state IN ('queued','running') LIMIT 1").fetchone():
            raise ValueError('仍有识别或批改任务运行，请等待完成。')
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'users' in tables or any(table not in tables for table in (*OWNED_TABLES, 'students')):
            raise ValueError('原数据库结构不符合单教师迁移要求。')
        if any('owner_id' in {column[1] for column in source.execute(f'PRAGMA table_info({table})')}
               for table in (*OWNED_TABLES, 'students')):
            raise ValueError('原数据库已存在教师归属，不能重复迁移。')
        target_data.parent.mkdir(parents=True, exist_ok=True)
        stage = target_data.with_name(target_data.name + '.staging-' + uuid4().hex[:8])
        stage.mkdir()
        try:
            with closing(sqlite3.connect(stage / 'dictation.sqlite3')) as copied:
                source.backup(copied)
            with closing(sqlite3.connect(stage / 'dictation.sqlite3')) as copied:
                copied.execute('PRAGMA foreign_keys=OFF')
                with copied:
                    copied.execute('''CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                        email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('teacher','platform_admin')), disabled_at INTEGER)''')
                    copied.execute('''CREATE TABLE user_sessions (digest TEXT PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        expires_at INTEGER NOT NULL)''')
                    copied.execute('INSERT INTO users VALUES (?,?,?,?,?,?)', teacher)
                    owner_id = teacher[0]
                    copied.execute('''CREATE TABLE students_new (
                        id INTEGER PRIMARY KEY, name TEXT NOT NULL, school_code TEXT,
                        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                        owner_id INTEGER NOT NULL REFERENCES users(id), UNIQUE(owner_id,school_code))''')
                    copied.execute('''INSERT INTO students_new
                        (id,name,school_code,active,created_at,owner_id)
                        SELECT id,name,school_code,active,created_at,? FROM students''', (owner_id,))
                    copied.execute('DROP TABLE students')
                    copied.execute('ALTER TABLE students_new RENAME TO students')
                    for table in OWNED_TABLES:
                        copied.execute(f'ALTER TABLE {table} ADD COLUMN owner_id INTEGER REFERENCES users(id)')
                        copied.execute(f'UPDATE {table} SET owner_id=?', (owner_id,))
                    copied.execute('DROP TABLE IF EXISTS teacher_sessions')
                    copied.execute('DROP TABLE IF EXISTS teacher_account')
                if copied.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or copied.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('迁移副本完整性检查未通过，原数据未修改。')
                names = {name for table in ('pages', 'batch_photos')
                         for row in copied.execute(f'SELECT original,image FROM {table}') for name in row}
            source_uploads = source_data / 'uploads'
            if not source_uploads.is_dir():
                raise FileNotFoundError('原照片目录不存在。')
            shutil.copytree(source_uploads, stage / 'uploads')
            if any(not (stage / 'uploads' / name).is_file() for name in names):
                raise FileNotFoundError('有答卷照片未找到；迁移已停止，原数据未修改。')
            stage.rename(target_data)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-data', required=True)
    parser.add_argument('--accounts-db', required=True)
    parser.add_argument('--target-data', required=True)
    parser.add_argument('--teacher-email', required=True)
    args = parser.parse_args()
    migrate(args.source_data, args.accounts_db, args.target_data, args.teacher_email)
    print('迁移副本已建立并校验；原数据未修改。')
