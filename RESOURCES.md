# Learning Resources

One resource per concept — the best one, not a list to scroll past.

---

## 1. What is an Eval?

**Start here before anything else.**

- **Hamel Husain — "Your AI Product Needs Evals"**  
  The clearest practical argument for why evals matter. Written by someone who debugs LLM products for a living.  
  Search: `hamel.dev your ai product needs evals`

- **Eugene Yan — "Evaluating LLMs"**  
  Patterns and taxonomy. Good overview of the full space.  
  Search: `eugeneyan.com evaluating llms`

---

## 2. Golden Datasets

- **"How to evaluate LLMs"** by Jason Wei (Google Brain)  
  Covers dataset design decisions: how many examples, what coverage, how to avoid contamination.  
  Search: `jason wei how to evaluate llms`

- **HELM paper** — Holistic Evaluation of Language Models  
  Academic benchmark design. Good reference for how to think about dataset composition.  
  https://arxiv.org/abs/2211.09110

---

## 3. Exact Match & Regex

No dedicated resource needed — the concept is simple.  
Read the BIG-Bench paper to see exact match used at scale and understand its limits.

- **BIG-Bench** — https://arxiv.org/abs/2206.04615

---

## 4. Embedding Similarity

**Read in this order:**

1. **"The Illustrated Word2Vec"** by Jay Alammar  
   Best visual intro to what embeddings actually are. Mandatory before writing any embedding code.  
   Search: `jalammar illustrated word2vec`

2. **sentence-transformers documentation**  
   Understand why sentence embeddings differ from word embeddings, and how `all-MiniLM-L6-v2` was trained.  
   https://www.sbert.net/

3. **Sentence-BERT paper** (if you want the theory)  
   https://arxiv.org/abs/1908.10084

**Key thing to understand:** cosine similarity measures the *angle* between vectors, not distance. Two vectors can be far apart in space but point in the same direction — that's what "semantically similar" means here.

---

## 5. LLM-as-Judge

**Read in this order:**

1. **"Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena"** — Zheng et al.  
   The paper that formalized this approach. Introduces the bias taxonomy you need to know.  
   https://arxiv.org/abs/2306.05685

2. **"Large Language Models are not Fair Evaluators"**  
   Specifically on position bias — judges score the first answer higher regardless of quality.  
   https://arxiv.org/abs/2305.17926

**The bias cheat sheet:**

| Bias | Description | Fix |
|------|-------------|-----|
| Verbosity | Longer = better (even if wrong) | Penalize length in prompt |
| Position | First answer wins | Swap order, average both runs |
| Self-preference | Claude favors Claude outputs | Use a different model as judge |
| Sycophancy | Confident tone = correct | Add "ignore confidence" to prompt |

---

## 6. Trajectory Eval

- **AgentBench** — the benchmark paper for evaluating LLM agents  
  Good reference for how trajectory correctness is defined across different agent tasks.  
  https://arxiv.org/abs/2308.03688

- **LCS algorithm** — what we use to score step sequences  
  https://en.wikipedia.org/wiki/Longest_common_subsequence

**The key insight:** final-output eval can pass even when the agent got lucky. Trajectory eval catches wrong reasoning that happened to produce a right answer.

---

## 7. Regression Detection & Baselines

- **"The ML Test Score: A Rubric for ML Production Readiness and Technical Debt Reduction"** by Breck et al. (Google, 2017)  
  Checklist for ML systems. Section on data/model monitoring is directly relevant.  
  Search: `ML Test Score Breck 2017 Google`  
  *(IEEE Big Data 2017 — not on arxiv)*

- **"Designing Machine Learning Systems"** by Chip Huyen — Chapter 8  
  Best practical treatment of distribution shift and regression detection. Worth buying.

**The statistical trap:** with small datasets (< 100 examples), a 5% score drop can be noise. Know your variance before setting thresholds.

---

## 8. CI Wiring

- **GitHub Actions docs** — https://docs.github.com/en/actions  
  Just the docs. This is plumbing, not a conceptual leap.

- **"Continuous Delivery for ML"** by Martin Fowler  
  Search: `martinfowler continuous delivery machine learning`

---

## 9. Meta-Eval (Eval Your Evals)

**This is the part most people skip. Don't.**

- **"FLASK: Fine-grained Language Model Evaluation"**  
  Shows how to measure whether your judge agrees with humans.  
  https://arxiv.org/abs/2307.10928

- **Cohen's Kappa** — the standard metric for inter-rater agreement  
  https://en.wikipedia.org/wiki/Cohen%27s_kappa  
  You want κ > 0.6 between your judge and human labels. Below that, your evals are unreliable.

---

## After You've Built Everything

These are the frameworks you'd use in production. Read their source code once you understand the concepts — you'll understand every design decision.

| Tool | What it does |
|------|-------------|
| [Braintrust](https://www.braintrust.dev/) | Eval + tracing platform |
| [Promptfoo](https://promptfoo.dev/) | Open source eval runner |
| [RAGAS](https://github.com/vibrantlabsai/ragas) | RAG-specific eval |
| [Evidently](https://www.evidentlyai.com/) | ML monitoring + regression |
| [LangSmith](https://smith.langchain.com/) | LangChain's eval/tracing layer |
