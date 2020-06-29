from collections import deque
import os

import lru
import numpy as np

import textattack
from textattack.attack_results import (
    FailedAttackResult,
    SkippedAttackResult,
    SuccessfulAttackResult,
)
from textattack.shared import AttackedText, utils


class Attack:
    """
    An attack generates adversarial examples on text. 
    
    This is an abstract class that contains main helper functionality for 
    attacks. An attack is comprised of a search method, goal function, 
    a transformation, and a set of one or more linguistic constraints that 
    successful examples must meet.

    Args:
        goal_function: A function for determining how well a perturbation is doing at achieving the attack's goal.
        constraints: A list of constraints to add to the attack, defining which perturbations are valid.
        transformation: The transformation applied at each step of the attack.
        search_method: A strategy for exploring the search space of possible perturbations
        constraint_cache_size (int): the number of items to keep in the constraints cache
    """

    def __init__(
        self,
        goal_function=None,
        constraints=[],
        transformation=None,
        search_method=None,
        constraint_cache_size=2 ** 18,
    ):
        """ Initialize an attack object. Attacks can be run multiple times. """
        self.goal_function = goal_function
        if not self.goal_function:
            raise NameError(
                "Cannot instantiate attack without self.goal_function for predictions"
            )
        self.search_method = search_method
        if not self.search_method:
            raise NameError("Cannot instantiate attack without search method")
        self.transformation = transformation
        if not self.transformation:
            raise NameError("Cannot instantiate attack without transformation")
        self.is_black_box = getattr(transformation, "is_black_box", True)

        if not self.search_method.check_transformation_compatibility(
            self.transformation
        ):
            raise ValueError(
                "SearchMethod {self.search_method} incompatible with transformation {self.transformation}"
            )

        self.constraints = []
        self.pre_transformation_constraints = []
        for constraint in constraints:
            if isinstance(
                constraint,
                textattack.constraints.pre_transformation.PreTransformationConstraint,
            ):
                self.pre_transformation_constraints.append(constraint)
            else:
                self.constraints.append(constraint)

        self.constraint_cache_size = constraint_cache_size
        self.constraints_cache = lru.LRU(constraint_cache_size)

        # Give search method access to functions for getting transformations and evaluating them
        self.search_method.get_transformations = self.get_transformations
        self.search_method.get_goal_results = self.goal_function.get_results
        self.search_method.filter_transformations = self.filter_transformations

    def get_transformations(self, current_text, original_text=None, **kwargs):
        """
        Applies ``self.transformation`` to ``text``, then filters the list of possible transformations
        through the applicable constraints.
        
        Args:
            current_text: The current ``AttackedText`` on which to perform the transformations.
            original_text: The original ``AttackedText`` from which the attack started.
            apply_constraints: Whether or not to apply post-transformation constraints.

        Returns:
            A filtered list of transformations where each transformation matches the constraints

        """
        if not self.transformation:
            raise RuntimeError(
                "Cannot call `get_transformations` without a transformation."
            )

        transformed_texts = np.array(
            self.transformation(
                current_text,
                pre_transformation_constraints=self.pre_transformation_constraints,
                **kwargs,
            )
        )
        return self.filter_transformations(
            transformed_texts, current_text, original_text
        )

    def _filter_transformations_uncached(
        self, transformed_texts, current_text, original_text=None
    ):
        """ 
        Filters a list of potential transformaed texts based on ``self.constraints``\.
        
        Args:
            transformed_texts: A list of candidate transformed ``AttackedText``\s to filter.
            current_text: The current ``AttackedText`` on which the transformation was applied.
            original_text: The original ``AttackedText`` from which the attack started.
        """
        filtered_texts = transformed_texts[:]
        for C in self.constraints:
            if len(filtered_texts) == 0:
                break
            filtered_texts = C.call_many(
                filtered_texts, current_text, original_text=original_text
            )
        # Default to false for all original transformations.
        for original_transformed_text in transformed_texts:
            self.constraints_cache[(current_text, original_transformed_text)] = False
        # Set unfiltered transformations to True in the cache.
        for filtered_text in filtered_texts:
            self.constraints_cache[(current_text, filtered_text)] = True
        return filtered_texts

    def filter_transformations(
        self, transformed_texts, current_text, original_text=None
    ):
        """ 
        Filters a list of potential transformed texts based on ``self.constraints``\.
        Checks cache first.
            
        Args:
            transformed_texts: A list of candidate transformed ``AttackedText``\s to filter.
            current_text: The current ``AttackedText`` on which the transformation was applied.
            original_text: The original ``AttackedText`` from which the attack started.
        """
        # Populate cache with transformed_texts
        uncached_texts = []
        for transformed_text in transformed_texts:
            if (current_text, transformed_text) not in self.constraints_cache:
                uncached_texts.append(transformed_text)
            else:
                # promote transformed_text to the top of the LRU cache
                self.constraints_cache[
                    (current_text, transformed_text)
                ] = self.constraints_cache[(current_text, transformed_text)]
        self._filter_transformations_uncached(
            uncached_texts, current_text, original_text=original_text
        )
        # Return transformed_texts from cache
        filtered_texts = [
            t for t in transformed_texts if self.constraints_cache[(current_text, t)]
        ]
        # Sort transformations to ensure order is preserved between runs
        filtered_texts.sort(key=lambda t: t.text)
        return filtered_texts

    def attack_one(self, initial_result):
        """
        Calls the ``SearchMethod`` to perturb the ``AttackedText`` stored in 
        ``initial_result``.

        Args:
            initial_result: The initial ``GoalFunctionResult`` from which to perturb.

        Returns:
            Either a ``SuccessfulAttackResult`` or ``FailedAttackResult``.
        """
        final_result = self.search_method(initial_result)
        if final_result.succeeded:
            return SuccessfulAttackResult(
                initial_result, final_result, self.goal_function.num_queries
            )
        else:
            return FailedAttackResult(
                initial_result, final_result, self.goal_function.num_queries
            )

    def _get_examples_from_dataset(self, dataset, indices=None):
        """ 
        Gets examples from a dataset and tokenizes them.

        Args:
            dataset: An iterable of (text, ground_truth_output) pairs
            indices: An iterable of indices of the dataset that we want to attack. If None, attack all samples in dataset.
        
        Returns:
            results (Iterable[GoalFunctionResult]): an iterable of GoalFunctionResults of the original examples
        """
        indices = indices if indices else deque(range(len(dataset)))
        if not isinstance(indices, deque):
            indices = deque(indices)
        if not indices:
            return
            yield

        while indices:
            i = indices.popleft()
            try:
                text, ground_truth_output = dataset[i]
                try:
                    # get label names from dataset, if possible
                    label_names = dataset.label_names
                except AttributeError:
                    label_names = None
                attacked_text = AttackedText(
                    text, attack_attrs={"label_names": label_names}
                )
                self.goal_function.num_queries = 0
                goal_function_result, _ = self.goal_function.get_result(
                    attacked_text, ground_truth_output
                )
                if goal_function_result.succeeded:
                    # Store the true output on the goal function so that the
                    # SkippedAttackResult has the correct output, not the incorrect.
                    goal_function_result.output = ground_truth_output
                yield goal_function_result

            except IndexError:
                raise IndexError(
                    f"Out of bounds access of dataset. Size of data is {len(dataset)} but tried to access index {i}"
                )

    def attack_dataset(self, dataset, indices=None):
        """ 
        Runs an attack on the given dataset and outputs the results to the 
        console and the output file.

        Args:
            dataset: An iterable of (text, ground_truth_output) pairs.
            indices: An iterable of indices of the dataset that we want to attack. If None, attack all samples in dataset.
        """

        examples = self._get_examples_from_dataset(dataset, indices=indices)

        for goal_function_result in examples:
            if goal_function_result.succeeded:
                yield SkippedAttackResult(goal_function_result)
            else:
                result = self.attack_one(goal_function_result)
                yield result

    def __repr__(self):
        """ 
        Prints attack parameters in a human-readable string.
            
        Inspired by the readability of printing PyTorch nn.Modules:
        https://github.com/pytorch/pytorch/blob/master/torch/nn/modules/module.py
        """
        main_str = "Attack" + "("
        lines = []

        lines.append(utils.add_indent(f"(search_method): {self.search_method}", 2))
        # self.goal_function
        lines.append(utils.add_indent(f"(goal_function):  {self.goal_function}", 2))
        # self.transformation
        lines.append(utils.add_indent(f"(transformation):  {self.transformation}", 2))
        # self.constraints
        constraints_lines = []
        constraints = self.constraints + self.pre_transformation_constraints
        if len(constraints):
            for i, constraint in enumerate(constraints):
                constraints_lines.append(utils.add_indent(f"({i}): {constraint}", 2))
            constraints_str = utils.add_indent("\n" + "\n".join(constraints_lines), 2)
        else:
            constraints_str = "None"
        lines.append(utils.add_indent(f"(constraints): {constraints_str}", 2))
        # self.is_black_box
        lines.append(utils.add_indent(f"(is_black_box):  {self.is_black_box}", 2))
        main_str += "\n  " + "\n  ".join(lines) + "\n"
        main_str += ")"
        return main_str

    __str__ = __repr__
