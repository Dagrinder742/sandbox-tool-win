import random

class Skill:
    def __init__(self):
        self.state = {}

    def perceive(self):
        self.state['temperature'] = random.uniform(20, 30)
        self.state['humidity'] = random.uniform(40, 60)
        self.state['light'] = random.choice(['day', 'night'])

    def reason(self):
        actions = []
        if self.state['temperature'] > 25:
            actions.append('cool')
        if self.state['humidity'] > 50:
            actions.append('ventilate')
        if self.state['light'] == 'day':
            actions.append('open curtains')
        return actions

    def evaluate(self, actions):
        scores = {action: random.randint(1, 10) for action in actions}
        return scores

    def act(self, chosen_action):
        print(f"Executing: {chosen_action}")

    def think(self):
        for _ in range(5):
            self.perceive()
            actions = self.reason()
            scores = self.evaluate(actions)
            chosen_action = max(scores, key=scores.get)
            self.act(chosen_action)

skill = Skill()
skill.think()
