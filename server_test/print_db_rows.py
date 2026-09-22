import sqlite3
p=r'c:\Users\senay\Documents\cred-entry_v_6.5\TCX-main\server_test\xml.db'
conn=sqlite3.connect(p)
cur=conn.execute('SELECT id,filename,cashier,amount,reference,source,date,fsnumber FROM xml_entries')
rows=cur.fetchall()
conn.close()
if not rows:
    print('NO ROWS')
else:
    for r in rows:
        print(r)
