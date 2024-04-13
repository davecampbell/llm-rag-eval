import argparse
import dspy
import glob
import json
import numpy as np
import os
import shutil

from dspy.evaluate import Evaluate
from dspy.teleprompt import BootstrapFewShotWithRandomSearch
from sklearn.model_selection import train_test_split

from .learning_utils import list_to_string, string_to_list, string_to_bool


DATA_DIR = "../data"
RESOURCE_DIR = "../resources"

DATASET_DIR = os.path.join(DATA_DIR, "dspy-datasets")
DATASET_FP = os.path.join(DATASET_DIR, "faithfulness.jsonl")
CONFIGS_DIR = os.path.join(RESOURCE_DIR, "configs")
BEST_CONFIG = os.path.join(CONFIGS_DIR, "faithfulness-best.json")


class QuestAnswerToFacts(dspy.Signature):
    """ Given a question-answer pair, generate a list of 3-5 facts
        from the answer
    """
    question: str = dspy.InputField(desc="a question")
    answer: str = dspy.InputField(desc="an answer")
    facts: str = dspy.OutputField(desc="a list of facts")


class ContextFactsToScore(dspy.Signature):
    """ Classify if fact can be inferred from context """
    context: str = dspy.InputField(desc="a context")
    fact: str = dspy.InputField(desc="a fact")
    score: bool = dspy.OutputField(
        desc="can fact be inferred from context? yes or no")


class Faithfulness(dspy.Module):
    def __init__(self):
        super().__init__()
        self.extractor = dspy.Predict(QuestAnswerToFacts)
        self.scorer = dspy.Predict(ContextFactsToScore)

    def forward(self, question: str, answer: str, context: str):
        facts = self.extractor(question=question, answer=answer).facts
        scores = []
        for fact in string_to_list(facts):
            can_infer = self.scorer(context=context, fact=fact).score
            scores.append(string_to_bool(can_infer, ["yes", "no"]))
        score = sum(scores) / len(scores)
        return dspy.Prediction(score=str(score))


def faithfulness_dataset(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError(
            f"Faithfulness dataset: {file_path} not found, "
            "create it with generate_datasets.py first.")
    examples = []
    with open(file_path, "r", encoding="utf-8") as fin:
        for line in fin:
            record = json.loads(line)
            question = record["question"]
            answer = record["answer"]
            context = list_to_string(record["context"], style="number")
            score = record["score"]
            examples.append(dspy.Example(
                question=question,
                answer=answer,
                context=context,
                score=str(score))
                .with_inputs("question", "answer", "context"))
    return examples


def faithfulness_metric(example, pred, trace=None):
    if trace is None:
        return 1.0 - abs(float(example.score) - float(pred.score))
    else:
        return float(pred.score)     # for inference


def optimize_prompt():

    config_paths = glob.glob(os.path.join(CONFIGS_DIR, "faithfulness-*.json"))

    if len(config_paths) == 0:
        teleprompter = BootstrapFewShotWithRandomSearch(
            metric=faithfulness_metric,
            max_bootstrapped_demos=2,
            max_labeled_demos=2,
            num_threads=1
        )
        examples = faithfulness_dataset(DATASET_FP)
        trainset, devset = train_test_split(examples, test_size=0.3,
                                            random_state=42)
        print(f"fact extractor dataset sizes: "
              f"{len(trainset)}, {len(devset)}, total: {len(examples)}")

        print("--- training ---")
        faithfulness = Faithfulness()
        faithfulness_opt = teleprompter.compile(
            faithfulness, trainset=trainset)
        ensemble = [prog for *_, prog in
                    faithfulness_opt.candidate_programs[:4]]
        
        os.makedirs(CONFIGS_DIR, exist_ok=True)
        for idx, prog in enumerate(ensemble):
            config_path = os.path.join(
                CONFIGS_DIR, f"faithfulness-{idx}.json")
            prog.save(config_path)

        print("--- evaluation ---")
        evaluate = Evaluate(devset=devset, metric=faithfulness_metric,
                            num_threads=1, display_progress=True)
        scores = [evaluate(prog) for prog in ensemble]
        print(f"Evaluation scores: {scores}")
        best_prompt_id = np.argmax(scores)
        shutil.copy(config_paths[best_prompt_id], BEST_CONFIG)

    prog = Faithfulness()
    prog.load(BEST_CONFIG)
    return prog


def compute_faithfulness(question: str,
                         answer: str,
                         context: str,
                         prompts_dict,
                         model):
    try:
        faithfulness_opt = prompts_dict["faithfulness"]
    except KeyError:
        faithfulness_opt = optimize_prompt()
        prompts_dict["faithfulness"] = faithfulness_opt
    pred = faithfulness_opt(
        question=question, answer=answer, context=context)
    return float(pred.score)
