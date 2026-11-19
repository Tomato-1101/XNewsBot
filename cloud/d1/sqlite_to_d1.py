"""本番 SQLite を D1 に取り込む SQL を作る（移行時に一度使う）。使い方: python3 sqlite_to_d1.py <db> cloud/d1/schema.sql <out.sql>"""
# D1 は unistr() を許さないので、文字列はそのままリテラルにして INSERT 文を作る
import sqlite3, sys
src, schema, out = sys.argv[1:4]
con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
def lit(v):
    if v is None: return "NULL"
    if isinstance(v, bytes): return "X'" + v.hex() + "'"
    if isinstance(v, (int, float)): return repr(v)
    return "'" + v.replace("'", "''") + "'"
with open(out, "w") as f:
    f.write("PRAGMA defer_foreign_keys = true;\n")
    f.write(open(schema).read() + "\n")
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    for t in tables:
        cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
        cl = ",".join(f'"{c}"' for c in cols)
        for row in con.execute(f'select {cl} from "{t}"'):
            f.write(f'INSERT INTO "{t}" ({cl}) VALUES ({",".join(lit(v) for v in row)});\n')
print("ok", tables)
