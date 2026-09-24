using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;

namespace QudGym.BridgeCore
{
    public enum BoundaryKind { Command, Prompt, Terminal }

    public sealed class DecisionFrame
    {
        public string DecisionId { get; }
        public long Turn { get; }
        public BoundaryKind Kind { get; }
        // Already serialized by the game-thread adapter. No mutable game objects cross threads.
        public string ObservationJson { get; }
        public DecisionFrame(string decisionId, long turn, BoundaryKind kind, string observationJson)
        {
            if (string.IsNullOrWhiteSpace(decisionId) || string.IsNullOrWhiteSpace(observationJson) || turn < 0)
                throw new ArgumentException("Invalid decision frame");
            DecisionId = decisionId; Turn = turn; Kind = kind; ObservationJson = observationJson;
        }
    }

    public sealed class PendingAction
    {
        public string RequestId { get; }
        public string ExpectedDecisionId { get; }
        public string ActionId { get; }
        internal CancellationToken Cancellation { get; }
        internal TaskCompletionSource<DecisionFrame> Completion { get; } =
            new TaskCompletionSource<DecisionFrame>(TaskCreationOptions.RunContinuationsAsynchronously);
        internal PendingAction(string requestId, string expectedDecisionId, string actionId, CancellationToken cancellation)
        {
            RequestId = requestId; ExpectedDecisionId = expectedDecisionId; ActionId = actionId;
            Cancellation = cancellation;
        }
    }

    /// <summary>
    /// Engine-neutral handoff, NOT a complete mod or a socket server.
    /// Network code may Submit; only the engine's turn thread may begin/complete/reconcile.
    /// The adapter must call Complete only after the submitted input was consumed and
    /// the engine is waiting at its next command/prompt, or the episode has terminated.
    /// An EndTurn/render callback alone is not proof of that boundary.
    /// </summary>
    public sealed class BoundaryQueue : IDisposable
    {
        private readonly object gate = new object();
        private readonly Queue<PendingAction> queue = new Queue<PendingAction>();
        private readonly int capacity;
        private PendingAction? active;
        private int? turnThread;
        private bool faulted;
        private bool disposed;

        public BoundaryQueue(int capacity = 32)
        {
            if (capacity < 1) throw new ArgumentOutOfRangeException(nameof(capacity));
            this.capacity = capacity;
        }

        public Task<DecisionFrame> Submit(string requestId, string expectedDecisionId, string actionId,
                                         CancellationToken cancellation = default)
        {
            if (string.IsNullOrWhiteSpace(requestId) || string.IsNullOrWhiteSpace(expectedDecisionId)
                || string.IsNullOrWhiteSpace(actionId)) throw new ArgumentException("Missing request field");
            lock (gate)
            {
                EnsureUsable();
                if (queue.Count + (active == null ? 0 : 1) >= capacity)
                    throw new InvalidOperationException("Queue capacity reached");
                var pending = new PendingAction(requestId, expectedDecisionId, actionId, cancellation);
                queue.Enqueue(pending);
                return pending.Completion.Task;
            }
        }

        public void InitializeTurnThread()
        {
            lock (gate)
            {
                EnsureUsable();
                int current = Thread.CurrentThread.ManagedThreadId;
                if (turnThread == null)
                {
                    turnThread = current;
                }
                else if (turnThread != current)
                {
                    throw new InvalidOperationException("Wrong game-turn thread");
                }
            }
        }

        public bool TryBeginOnTurnThread(string currentDecisionId, out PendingAction? pending)
        {
            lock (gate)
            {
                EnsureTurnThread(); EnsureUsable();
                pending = null;
                if (active != null) return false;
                while (queue.Count != 0)
                {
                    var next = queue.Dequeue();
                    if (next.Cancellation.IsCancellationRequested)
                    {
                        next.Completion.TrySetCanceled();
                        continue;
                    }
                    if (next.ExpectedDecisionId != currentDecisionId)
                    {
                        next.Completion.TrySetException(new InvalidOperationException("stale_decision"));
                        continue;
                    }
                    active = next; pending = next;
                    return true;
                }
                return false;
            }
        }

        public void CompleteOnTurnThread(PendingAction pending, DecisionFrame frame)
        {
            lock (gate)
            {
                EnsureTurnThread(); EnsureActive(pending);
                if (frame.DecisionId == pending.ExpectedDecisionId)
                    throw new InvalidOperationException("Input has not reached a new decision boundary");
                active = null;
                // Cancellation after dispatch cannot promise rollback. Resolve the actual outcome.
                pending.Completion.TrySetResult(frame);
            }
        }

        public void FailAfterDispatchOnTurnThread(PendingAction pending, Exception error)
        {
            lock (gate)
            {
                EnsureTurnThread(); EnsureActive(pending);
                active = null; faulted = true;
                pending.Completion.TrySetException(error);
                while (queue.Count != 0)
                    queue.Dequeue().Completion.TrySetException(new InvalidOperationException("worker_faulted"));
            }
        }

        public void ReconcileOnTurnThread(DecisionFrame confirmedFrame)
        {
            if (confirmedFrame == null) throw new ArgumentNullException(nameof(confirmedFrame));
            lock (gate)
            {
                EnsureTurnThread();
                if (disposed) throw new ObjectDisposedException(nameof(BoundaryQueue));
                if (active != null) throw new InvalidOperationException("Input remains in flight");
                faulted = false;
            }
        }

        private void EnsureTurnThread()
        {
            int current = Thread.CurrentThread.ManagedThreadId;
            if (turnThread == null)
                throw new InvalidOperationException("Turn thread is not initialized");
            if (turnThread != current) throw new InvalidOperationException("Wrong game-turn thread");
        }
        private void EnsureUsable()
        {
            if (disposed) throw new ObjectDisposedException(nameof(BoundaryQueue));
            if (faulted) throw new InvalidOperationException("worker_faulted");
        }
        private void EnsureActive(PendingAction pending)
        {
            EnsureUsable();
            if (!ReferenceEquals(active, pending)) throw new InvalidOperationException("Uncorrelated completion");
        }
        public void Dispose()
        {
            lock (gate)
            {
                if (disposed) return;
                disposed = true;
                active?.Completion.TrySetException(new ObjectDisposedException(nameof(BoundaryQueue)));
                active = null;
                while (queue.Count != 0)
                    queue.Dequeue().Completion.TrySetException(new ObjectDisposedException(nameof(BoundaryQueue)));
            }
        }
    }
}
