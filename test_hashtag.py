from ig_client import get_client
cl = get_client()
print("session ok")
medias = cl.hashtag_medias_top("فال_روزانه", amount=9)
seen = {}
for m in medias:
    seen.setdefault(m.user.username, 0); seen[m.user.username] += 1
print(f"{len(medias)} posts, {len(seen)} accounts:")
for u, n in seen.items(): print(" ", u, n)
