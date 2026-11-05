import random


class Skill:
    def __init__(self):
        self.state = {"metrics": {}, "history": []}

    def perceive(self):
        self.state["metrics"] = {"temperature": random.randint(10, 30), "humidity": random.randint(40, 80), "light": random.randint(0, 100)}
        print("Perceive:", self.state["metrics"])
        return self.state["metrics"]

    def reason(self):
        m = self.state["metrics"]
        actions = [f"cool to {m['temperature'] - 2}", f"dry to {m['humidity'] - 5}", f"dim light {m['light']}"]
        print("Reason:", actions)
        return actions

    def evaluate(self, actions):
        scores = {a: len(a) for a in actions}
        best = actions[0]
        for a in actions:
            if scores[a] > scores[best]:
                best = a
        print("Evaluate:", scores)
        return best

    def act(self, action):
        print("Act:", action)
        return action

    def think(self, rounds=3):
        for _ in range(rounds):
            self.perceive()
            chosen = self.act(self.evaluate(self.reason()))
            self.state["history"].append(chosen)
        return self.state["history"]


if __name__ == "__main__":
    Skill().think()
