"""
Optimization Engine — автоматический подбор параметров стратегии.

Поддерживает:
- Grid search — перебор всех комбинаций параметров
- Random search — случайные комбинации
- Простой Bayesian-like search (через weighted sampling лучших результатов)
"""

import random
import itertools
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass
from loguru import logger

from core.backtest_engine import BacktestEngine, BacktestResult


@dataclass
class OptimizationResult:
    """Результат оптимизации."""
    best_params: Dict[str, Any]
    best_score: float
    all_results: List[Tuple[Dict[str, Any], float]]
    total_runs: int
    elapsed_seconds: float
    method: str


class OptimizationEngine:
    """
    Движок оптимизации параметров стратегии.
    
    Пример использования:
        engine = OptimizationEngine()
        result = engine.grid_search(
            module=strategy_module,
            filepath='data/strategy.txt',
            param_grid={'fast_period': [5, 10, 15], 'slow_period': [20, 30, 40]},
            connector_id='finam',
            board='TQBR',
        )
        print(f'Лучшие параметры: {result.best_params}')
        print(f'Лучший результат: {result.best_score}')
    """

    def __init__(self, backtest_engine: Optional[BacktestEngine] = None):
        self._backtest_engine = backtest_engine or BacktestEngine()
        self._stop_flag: Optional[Callable[[], bool]] = None

    def set_stop_flag(self, flag: Callable[[], bool]):
        """Установить флаг остановки оптимизации."""
        self._stop_flag = flag

    def grid_search(
        self,
        module,
        filepath: str,
        param_grid: Dict[str, List[Any]],
        connector_id: str = 'finam',
        board: str = 'TQBR',
        score_fn: Optional[Callable[[BacktestResult], float]] = None,
    ) -> OptimizationResult:
        """
        Grid search — полный перебор всех комбинаций параметров.
        
        Args:
            module: Модуль стратегии
            filepath: Путь к файлу с данными
            param_grid: Словарь {param_name: [values]}
            connector_id: ID коннектора
            board: Код режима торгов
            score_fn: Функция оценки результата (по умолчанию total_net_pnl)
        """
        logger.info(f'[OptimizationEngine] Grid search: {len(param_grid)} параметров')

        # Генерируем все комбинации
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))
        total = len(combinations)
        logger.info(f'[OptimizationEngine] Всего комбинаций: {total}')

        return self._run_search(
            module=module,
            filepath=filepath,
            combinations=combinations,
            keys=keys,
            connector_id=connector_id,
            board=board,
            score_fn=score_fn,
            method='grid',
        )

    def random_search(
        self,
        module,
        filepath: str,
        param_grid: Dict[str, List[Any]],
        n_iterations: int = 50,
        connector_id: str = 'finam',
        board: str = 'TQBR',
        score_fn: Optional[Callable[[BacktestResult], float]] = None,
        random_seed: Optional[int] = None,
    ) -> OptimizationResult:
        """
        Random search — случайные комбинации параметров.
        
        Args:
            module: Модуль стратегии
            filepath: Путь к файлу с данными
            param_grid: Словарь {param_name: [values]}
            n_iterations: Количество итераций
            connector_id: ID коннектора
            board: Код режима торгов
            score_fn: Функция оценки результата
            random_seed: Seed для воспроизводимости
        """
        if random_seed is not None:
            random.seed(random_seed)

        logger.info(f'[OptimizationEngine] Random search: {n_iterations} итераций')

        keys = list(param_grid.keys())
        values = list(param_grid.values())

        # Генерируем случайные комбинации
        combinations = []
        for _ in range(n_iterations):
            combo = [random.choice(v) for v in values]
            combinations.append(combo)

        return self._run_search(
            module=module,
            filepath=filepath,
            combinations=combinations,
            keys=keys,
            connector_id=connector_id,
            board=board,
            score_fn=score_fn,
            method='random',
        )

    def bayesian_search(
        self,
        module,
        filepath: str,
        param_grid: Dict[str, List[Any]],
        n_iterations: int = 50,
        n_initial: int = 10,
        connector_id: str = 'finam',
        board: str = 'TQBR',
        score_fn: Optional[Callable[[BacktestResult], float]] = None,
        random_seed: Optional[int] = None,
    ) -> OptimizationResult:
        """
        Упрощённый Bayesian search — weighted sampling лучших результатов.
        
        После начальных случайных итераций, новые параметры выбираются
        с весом, пропорциональным успешности предыдущих результатов.
        """
        if random_seed is not None:
            random.seed(random_seed)

        logger.info(f'[OptimizationEngine] Bayesian search: {n_iterations} итераций')

        keys = list(param_grid.keys())
        values = list(param_grid.values())

        # Начальные случайные итерации
        initial_combinations = []
        for _ in range(n_initial):
            combo = [random.choice(v) for v in values]
            initial_combinations.append(combo)

        # Запускаем начальные итерации
        results: List[Tuple[Dict[str, Any], float]] = []
        for combo in initial_combinations:
            params = dict(zip(keys, combo))
            score = self._run_backtest(module, filepath, params, connector_id, board, score_fn)
            results.append((params, score))

        # Итеративный поиск
        remaining = n_iterations - n_initial
        for i in range(remaining):
            if self._stop_flag and self._stop_flag():
                logger.info('[OptimizationEngine] Остановка по флагу')
                break

            combo = self._sample_from_best(results, param_grid, keys)
            params = dict(zip(keys, combo))
            score = self._run_backtest(module, filepath, params, connector_id, board, score_fn)
            results.append((params, score))

        best_params, best_score = max(results, key=lambda x: x[1]) if results else ({}, 0.0)

        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            all_results=results,
            total_runs=len(results),
            elapsed_seconds=0.0,
            method='bayesian',
        )

    def _run_search(
        self,
        module,
        filepath: str,
        combinations: List[List[Any]],
        keys: List[str],
        connector_id: str,
        board: str,
        score_fn: Optional[Callable[[BacktestResult], float]],
        method: str,
    ) -> OptimizationResult:
        """Запустить поиск по заданным комбинациям."""
        start_time = time.time()
        results: List[Tuple[Dict[str, Any], float]] = []

        for i, combo in enumerate(combinations):
            if self._stop_flag and self._stop_flag():
                logger.info(f'[OptimizationEngine] Остановка по флагу на итерации {i}')
                break

            params = dict(zip(keys, combo))
            score = self._run_backtest(module, filepath, params, connector_id, board, score_fn)
            results.append((params, score))

            if (i + 1) % 10 == 0:
                elapsed = time.time() - start_time
                best_score = max(r[1] for r in results)
                logger.info(
                    f'[OptimizationEngine] {method} {i + 1}/{len(combinations)}: '
                    f'best={best_score:,.2f}, elapsed={elapsed:.1f}s'
                )

        elapsed = time.time() - start_time
        best_params, best_score = max(results, key=lambda x: x[1]) if results else ({}, 0.0)

        return OptimizationResult(
            best_params=best_params,
            best_score=best_score,
            all_results=results,
            total_runs=len(results),
            elapsed_seconds=elapsed,
            method=method,
        )

    def _run_backtest(
        self,
        module,
        filepath: str,
        params: Dict[str, Any],
        connector_id: str,
        board: str,
        score_fn: Optional[Callable[[BacktestResult], float]],
    ) -> float:
        """Запустить бэктест с заданными параметрами и вернуть score."""
        original_defaults = {}
        if hasattr(module, 'get_params'):
            raw_params = module.get_params()
            for key, value in params.items():
                if key in raw_params:
                    original_defaults[key] = raw_params[key]['default']
                    raw_params[key]['default'] = value

        try:
            result = self._backtest_engine.run(
                module=module,
                filepath=filepath,
                connector_id=connector_id,
                board=board,
                stop_flag=self._stop_flag,
            )

            if score_fn:
                return score_fn(result)
            else:
                return result.total_net_pnl
        except Exception as e:
            logger.warning(f'[OptimizationEngine] Ошибка бэктеста с params={params}: {e}')
            return float('-inf')
        finally:
            if hasattr(module, 'get_params'):
                raw_params = module.get_params()
                for key, orig_value in original_defaults.items():
                    if key in raw_params:
                        raw_params[key]['default'] = orig_value

    def _sample_from_best(
        self,
        results: List[Tuple[Dict[str, Any], float]],
        param_grid: Dict[str, List[Any]],
        keys: List[str],
    ) -> List[Any]:
        """Выбрать параметры на основе weighted sampling лучших результатов."""
        if not results:
            return [random.choice(v) for v in param_grid.values()]

        sorted_results = sorted(results, key=lambda x: x[1], reverse=True)
        top_n = max(1, len(sorted_results) // 3)
        top_results = sorted_results[:top_n]

        combo = []
        for key in keys:
            value_scores: Dict[Any, float] = {}
            for params, score in top_results:
                val = params.get(key)
                if val not in value_scores:
                    value_scores[val] = 0.0
                value_scores[val] += max(0, score)

            if value_scores:
                values = list(value_scores.keys())
                weights = list(value_scores.values())
                total_weight = sum(weights)
                if total_weight > 0:
                    weights = [w / total_weight for w in weights]
                    chosen = random.choices(values, weights=weights, k=1)[0]
                else:
                    chosen = random.choice(values)
            else:
                chosen = random.choice(param_grid[key])

            combo.append(chosen)

        return combo


# Синглтон для использования в приложении
optimization_engine: OptimizationEngine = OptimizationEngine()
