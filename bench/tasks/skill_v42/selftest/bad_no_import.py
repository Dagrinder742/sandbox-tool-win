
class Skill:
    def __init__(self):
        self.state = {"metrics": {}, "history": [], "energy": 100, "cycles": 0}

    def perceive(self):
        self.state["metrics"] = {
            "temperature": random.randint(10, 30),
            "humidity": random.randint(40, 80),
            "light": random.randint(0, 100),
        }
        print("Perceive: ", self.state["metrics"])
        return self.state["metrics"]

    def reason(self, metrics):
        actions = ["scout", "patrol", "rest"]
        if metrics["temperature"] > 25:
            actions[0] = "cool_down"
        if metrics["light"] < 20:
            actions[1] = "illuminate"
        print("Reason: ", actions)
        return actions

    def evaluate(self, actions):
        scores = {a: len(a) for a in actions}
        best = max(scores.values())
        choice = [a for a, s in scores.items() if s == best][0]
        print("Evaluate: ", scores)
        return choice

    def act(self, action):
        self.state["energy"] = max(0, self.state["energy"] - 15)
        self.state["cycles"] += 1
        print("Act: ", action, "energy:", self.state["energy"])
        return action

    def think(self, rounds=3):
        for _ in range(rounds):
            metrics = self.perceive()
            actions = self.reason(metrics)
            choice = self.evaluate(actions)
            self.act(choice)
            self.state["history"].append(choice)
            if self.state["energy"] == 0:
                break
        return self.state["history"]

    def report(self):
        r = {"total_cycles": self.state["cycles"], "remaining_energy": self.state["energy"],
             "actions_taken": len(self.state["history"])}
        print("Report: ", r)
        return r


if __name__ == "__main__":
    s = Skill()
    s.think()
    s.report()
