import unittest

from panopticon_py.correlation_rolling import align_series, pairwise_correlation_edges, pearson_rho


class CorrelationRollingTests(unittest.TestCase):
    def test_pearson_perfect_linear(self) -> None:
        a = [float(i) for i in range(20)]
        b = [2.0 * x + 0.5 for x in a]
        r = pearson_rho(a, b)
        self.assertIsNotNone(r)
        assert r is not None
        self.assertAlmostEqual(r, 1.0, places=5)

    def test_pairwise_edges(self) -> None:
        s = align_series({"m1": [1, 2, 3, 4, 5, 6], "m2": [2, 4, 6, 8, 10, 12]})
        edges = pairwise_correlation_edges(s, window_sec=300, epsilon=0.5)
        self.assertTrue(len(edges) >= 1)


if __name__ == "__main__":
    unittest.main()
