from typing import *

import numpy as np

from asm2vec.asm import Instruction
from asm2vec.internal.representative import FunctionRepository
from asm2vec.internal.representative import VectorizedFunction
from asm2vec.internal.representative import VectorizedToken
from asm2vec.internal.sampling import NegativeSampler
from asm2vec.internal.typing import InstructionSequence
from asm2vec.internal.typing import Vocabulary


class Asm2VecParams:
    def __init__(self, **kwargs):
        self.d: int = kwargs.get('d', 200)
        self.initial_alpha: float = kwargs.get('alpha', 0.05)
        self.alpha_update_interval: int = kwargs.get('alpha_update_interval', 10000)
        self.num_of_rnd_walks: int = kwargs.get('rnd_walks', 3)
        self.neg_samples: int = kwargs.get('neg_samples', 25)
        self.iteration: int = kwargs.get('iteration', 1)


class SequenceWindow:
    ViewType = Tuple[VectorizedToken, List[VectorizedToken],
                     VectorizedToken, List[VectorizedToken],
                     VectorizedToken, List[VectorizedToken]]

    def __init__(self, sequence: InstructionSequence, vocabulary: Vocabulary):
        self._seq = sequence
        self._vocab = vocabulary
        self._i = 1

    def __iter__(self):
        return self

    def __next__(self) -> ViewType:
        if self._i >= len(self._seq) - 1:
            raise StopIteration()

        def token_lookup(name) -> VectorizedToken:
            return self._vocab[name].vectorized()

        self._prev_ins = self._seq[self._i - 1]
        self._curr_ins = self._seq[self._i]
        self._next_ins = self._seq[self._i + 1]

        self._prev_ins_op = token_lookup(self._prev_ins.op())
        self._prev_ins_args = list(map(token_lookup, self._prev_ins.args()))
        self._curr_ins_op = token_lookup(self._curr_ins.op())
        self._curr_ins_args = list(map(token_lookup, self._curr_ins.args()))
        self._next_ins_op = token_lookup(self._next_ins.op())
        self._next_ins_args = list(map(token_lookup, self._next_ins.args()))

        result = (self._prev_ins_op, self._prev_ins_args,
                  self._curr_ins_op, self._curr_ins_args,
                  self._next_ins_op, self._next_ins_args)

        self._i += 1

        return result

    def prev_ins(self) -> Instruction:
        return self._prev_ins

    def prev_ins_op(self) -> VectorizedToken:
        return self._prev_ins_op

    def prev_ins_args(self) -> List[VectorizedToken]:
        return self._prev_ins_args

    def curr_ins(self) -> Instruction:
        return self._curr_ins

    def curr_ins_op(self) -> VectorizedToken:
        return self._curr_ins_op

    def curr_ins_args(self) -> List[VectorizedToken]:
        return self._curr_ins_args

    def next_ins(self) -> Instruction:
        return self._next_ins

    def next_ins_op(self) -> VectorizedToken:
        return self._next_ins_op

    def next_ins_args(self) -> List[VectorizedToken]:
        return self._next_ins_args


class TrainingContext:
    class Counter:
        def __init__(self, name: str, initial: int = 0):
            self._name = name
            self._val = initial

        def val(self) -> int:
            return self._val

        def inc(self) -> int:
            self._val += 1
            return self._val

        def reset(self) -> int:
            v = self._val
            self._val = 0
            return v

    TOKENS_HANDLED_COUNTER: str = "tokens_handled"

    def __init__(self, repo: FunctionRepository, params: Asm2VecParams, is_estimating: bool = False):
        self._repo = repo
        self._params = params
        self._alpha = params.initial_alpha
        self._sampler = NegativeSampler(map(lambda t: (t, t.frequency), repo.vocab().values()))
        self._is_estimating = is_estimating
        self._counters = dict()

    def repo(self) -> FunctionRepository:
        return self._repo

    def params(self) -> Asm2VecParams:
        return self._params

    def alpha(self) -> float:
        return self._alpha

    def set_alpha(self, alpha: float) -> None:
        self._alpha = alpha

    def sampler(self) -> NegativeSampler:
        return self._sampler

    def is_estimating(self) -> bool:
        return self._is_estimating

    def create_sequence_window(self, seq: InstructionSequence) -> SequenceWindow:
        return SequenceWindow(seq, self._repo.vocab())

    def get_counter(self, name: str) -> Counter:
        return self._counters.get(name)

    def add_counter(self, name: str, initial: int = 0) -> Counter:
        c = self.__class__.Counter(name, initial)
        self._counters[name] = c
        return c


def _sigmoid(x: float) -> float:
    e = np.exp(x)
    return e / (1 + e)


def _identity(cond: bool) -> int:
    return 1 if cond else 0


def _dot_sigmoid(lhs: np.ndarray, rhs: np.ndarray) -> float:
    # Suppress PyTypeChecker for the following statement since the type checker gives wrong result.
    # noinspection PyTypeChecker
    return _sigmoid(np.dot(lhs, rhs))


def _get_inst_repr(op: VectorizedToken, args: Iterable[VectorizedToken]) -> np.ndarray:
    return np.hstack(op.v, np.average(list(map(lambda tk: tk.v, args)), axis=0))


def _get_function_grad(samples: Iterable[VectorizedToken], current_token: VectorizedToken,
                       delta: np.ndarray) -> np.ndarray:
    neu = np.sum(list(map(
        lambda t: _identity(t == current_token) - _dot_sigmoid(current_token.v_pred, delta),
        samples)), axis=0)
    return neu / 3 * current_token.v_pred


def _get_target_grad(target: VectorizedToken, current_token: VectorizedToken, delta: np.ndarray) -> np.ndarray:
    return _identity(target == current_token) - _dot_sigmoid(target.v_pred, delta) * delta


def _train_vectorized(wnd: SequenceWindow, f: VectorizedFunction, context: TrainingContext) -> None:
    ct_prev = _get_inst_repr(wnd.prev_ins_op(), wnd.prev_ins_args())
    ct_next = _get_inst_repr(wnd.next_ins_op(), wnd.next_ins_args())
    delta = np.average((ct_prev, f, ct_next))

    tokens = [wnd.curr_ins_op()] + wnd.curr_ins_args()

    f_grad = np.zeros(f.v.shape)
    for tk in tokens:
        targets: Dict[str, VectorizedToken] = \
            dict(map(lambda x: (x.name(), x.vectorized()), context.sampler().sample(context.params().neg_samples)))
        if tk.name() not in targets:
            targets[tk.name()] = tk

        tokens_handled_counter = context.get_counter(TrainingContext.TOKENS_HANDLED_COUNTER)
        if tokens_handled_counter.val() % context.params().alpha_update_interval == 0:
            # Update the learning rate.
            alpha = 1 - tokens_handled_counter.val() / (
                    context.params().iteration * context.repo().num_of_tokens() + 1)
            context.set_alpha(max(alpha, context.params().initial_alpha * 0.0001))

        # Compute and accumulate gradient of function vector.
        f_grad += _get_function_grad(targets.values(), tk, delta)

        if not context.is_estimating():
            # Compute and apply gradient of current token's vector.
            for t in targets.values():
                t.v_pred -= context.alpha() * _get_target_grad(t, tk, delta)

    # Apply function gradient.
    f.v -= context.alpha() * f_grad

    if not context.is_estimating():
        # Apply instruction gradient.
        d = len(wnd.prev_ins_op().v)

        wnd.prev_ins_op().v -= context.alpha() * f_grad[:d]
        prev_args_grad = f_grad[d:] / len(wnd.prev_ins_args()) * context.alpha()
        for t in wnd.prev_ins_args():
            t.v -= prev_args_grad

        wnd.next_ins_op().v -= context.params().initial_alpha * f_grad[:d]
        next_args_grad = f_grad[d:] / len(wnd.next_ins_args()) * context.alpha()
        for t in wnd.next_ins_args():
            t.v -= next_args_grad


def _train_sequence(f: VectorizedFunction, seq: InstructionSequence, context: TrainingContext) -> None:
    wnd = context.create_sequence_window(seq)

    try:
        while True:
            next(wnd)
            _train_vectorized(wnd, f, context)
    except StopIteration:
        pass


def train(repository: FunctionRepository, params: Asm2VecParams) -> None:
    context = TrainingContext(repository, params)
    context.add_counter(TrainingContext.TOKENS_HANDLED_COUNTER)

    for f in context.repo().funcs():
        for seq in f.sequential().sequences():
            _train_sequence(f, seq, context)


def estimate(f: VectorizedFunction, estimate_repo: FunctionRepository, params: Asm2VecParams) -> np.ndarray:
    context = TrainingContext(estimate_repo, params, True)
    for seq in f.sequential().sequences():
        _train_sequence(f, seq, context)

    return f.v