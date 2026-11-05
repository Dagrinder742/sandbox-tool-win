def count_words(text):
    counts = {}
    for raw in text.lower().split():
        w = raw.strip('.,!?;:"\'()')
        if w:
            counts[w] = counts.get(w, 0) + 1
    return counts


def top_words(text, n):
    items = sorted(count_words(text).items(), key=lambda kv: (-kv[1], kv[0]))
    return items[:n]


if __name__ == "__main__":
    print(top_words("the cat and the hat and the bat", 2))
