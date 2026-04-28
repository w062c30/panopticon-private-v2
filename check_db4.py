import sqlite3
conn = sqlite3.connect('data/panopticon.db')
cur = conn.cursor()

# Check consensus markets - what slugs do we have?
cur.execute("SELECT slug, wallet_count FROM consensus_markets ORDER BY wallet_count DESC LIMIT 10")
print('consensus_markets:')
for row in cur.fetchall():
    print(f'  {row}')

# Check discovered_entities with primary_tag
cur.execute("SELECT COUNT(*) FROM discovered_entities WHERE primary_tag != ''")
tagged = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM discovered_entities")
total = cur.fetchone()[0]
print(f'\ndiscovered_entities: {tagged} with primary_tag, {total} total')

# Check distinct wallets
cur.execute("SELECT COUNT(DISTINCT address) FROM wallet_observations WHERE obs_type='clob_trade'")
distinct_wallets = cur.fetchone()[0]
print(f'distinct wallets with clob_trade: {distinct_wallets}')

# Check market_id overlap between clob_trade and entropy_drop
cur.execute("""
SELECT COUNT(DISTINCT w1.market_id) 
FROM wallet_observations w1 
JOIN wallet_observations w2 ON w1.market_id = w2.market_id 
WHERE w1.obs_type='clob_trade' AND w2.obs_type='entropy_drop'
""")
overlap = cur.fetchone()[0]
print(f'markets with both clob_trade and entropy_drop: {overlap}')

# Check how many clob_trade markets have >= 2 distinct wallets
cur.execute("""
SELECT COUNT(*) FROM (
    SELECT market_id FROM wallet_observations 
    WHERE obs_type='clob_trade' 
    GROUP BY market_id 
    HAVING COUNT(DISTINCT address) >= 2
)
""")
multi_wallet_markets = cur.fetchone()[0]
print(f'markets with >= 2 distinct clob_trade wallets: {multi_wallet_markets}')

# Check polymarket_link_map
cur.execute("SELECT COUNT(*) FROM polymarket_link_map")
print(f'\npolymarket_link_map rows: {cur.fetchone()[0]}')

cur.execute("SELECT market_id, event_slug FROM polymarket_link_map LIMIT 5")
print('polymarket_link_map samples:')
for row in cur.fetchall():
    print(f'  {row}')

conn.close()