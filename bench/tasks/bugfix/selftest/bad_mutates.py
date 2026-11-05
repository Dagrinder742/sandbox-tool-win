def average(numbers):
    if not numbers:
        return 0.0
    return sum(numbers) / len(numbers)


def median(numbers):
    s = numbers
    s.sort()
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


if __name__ == "__main__":
    print(average([1, 2, 3, 4]))
    print(median([1, 2, 3, 4]))
    print(average([]))
