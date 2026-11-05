def count_words(text):
    text = text.lower()
    text = text.strip('.,!?;:"\'() ')
    counts = {}
    for word in text.split():
        if word:
            counts[word] = counts.get(word, 0) + 1
    return counts


def top_words(text, n):
    items = sorted(count_words(text).items(), key=lambda kv: (-kv[1], kv[0]))
    return items[:n]


if __name__ == "__main__":
    print(top_words("the cat and the hat and the bat", 2))
