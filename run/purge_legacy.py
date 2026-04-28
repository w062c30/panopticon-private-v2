import sqlite3

conn = sqlite3.connect("data/panopticon.db")

# Count before
before_exec = conn.execute(
    "SELECT COUNT(*) FROM execution_records WHERE accepted=1"
).fetchone()[0]
before_settle = conn.execute(
    "SELECT COUNT(*) FROM realized_pnl_settlement"
).fetchone()[0]
before_zero_entry = conn.execute(
    "SELECT COUNT(*) FROM execution_records WHERE accepted=1 AND avg_entry_price=0.0"
).fetchone()[0]
before_null_exit = conn.execute(
    "SELECT COUNT(*) FROM realized_pnl_settlement WHERE entry_price=0.5 AND exit_price IS NULL"
).fetchone()[0]

print(f"Before purge:")
print(f"  execution_records accepted=1:   {before_exec}")
print(f"    of which avg_entry_price=0.0: {before_zero_entry}")
print(f"  realized_pnl_settlement:         {before_settle}")
print(f"    entry=0.5 exit=NULL:          {before_null_exit}")

# Purge execution_records: accepted=1 AND avg_entry_price=0.0
del_exec = conn.execute("""
    DELETE FROM execution_records
    WHERE accepted=1 AND avg_entry_price=0.0
""").rowcount

# Purge realized_pnl_settlement: entry_price=0.5 AND exit_price IS NULL
del_settle = conn.execute("""
    DELETE FROM realized_pnl_settlement
    WHERE entry_price=0.5 AND exit_price IS NULL
""").rowcount

conn.commit()

# Verify clean state
after_exec = conn.execute(
    "SELECT COUNT(*) FROM execution_records WHERE accepted=1"
).fetchone()[0]
after_settle = conn.execute(
    "SELECT COUNT(*) FROM realized_pnl_settlement"
).fetchone()[0]
after_zero_entry = conn.execute(
    "SELECT COUNT(*) FROM execution_records WHERE accepted=1 AND avg_entry_price=0.0"
).fetchone()[0]
after_null_exit = conn.execute(
    "SELECT COUNT(*) FROM realized_pnl_settlement WHERE exit_price IS NULL"
).fetchone()[0]
remaining_with_real_price = conn.execute(
    "SELECT COUNT(*) FROM execution_records WHERE accepted=1 AND avg_entry_price > 0"
).fetchone()[0]

conn.close()

print(f"\nAfter purge:")
print(f"  execution_records accepted=1:   {after_exec} (deleted {del_exec})")
print(f"    remaining with avg_entry_price=0.0: {after_zero_entry}")
print(f"    remaining with real entry price:   {remaining_with_real_price}")
print(f"  realized_pnl_settlement:  {after_settle} (deleted {del_settle})")
print(f"    remaining with exit_price=NULL:    {after_null_exit}")

if after_zero_entry == 0 and after_null_exit <= 2:
    print("\nCLEAN -- legacy records purged successfully")
else:
    print(f"\nUNEXPECTED -- zero_entry={after_zero_entry} null_exit={after_null_exit}")
