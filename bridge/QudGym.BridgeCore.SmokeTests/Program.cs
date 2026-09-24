using System;
using System.Threading;
using System.Threading.Tasks;
using QudGym.BridgeCore;

static class Program
{
    static void Check(bool condition, string message)
    { if (!condition) throw new Exception(message); }

    static void Main()
    {
        using var queue = new BoundaryQueue(2);
        bool rejectedBeforeInitialization = Task.Run(() => {
            try { queue.TryBeginOnTurnThread("d1", out _); return false; }
            catch (InvalidOperationException) { return true; }
        }).GetAwaiter().GetResult();
        Check(rejectedBeforeInitialization, "Queue must require explicit turn-thread initialization");
        queue.InitializeTurnThread();
        var result = queue.Submit("r1", "d1", "move:E");
        Check(!result.IsCompleted, "Enqueue must not execute game logic");
        Check(queue.TryBeginOnTurnThread("d1", out var command), "Expected pending input");
        Check(!result.IsCompleted, "Dispatch is not completion");
        queue.CompleteOnTurnThread(command!, new DecisionFrame("d2", 0, BoundaryKind.Prompt, "{}"));
        Check(result.Result.Kind == BoundaryKind.Prompt, "Zero-turn prompt is a valid boundary");
        var stale = queue.Submit("r2", "d1", "wait");
        Check(!queue.TryBeginOnTurnThread("d2", out _), "Reject stale cursor");
        Check(stale.IsFaulted, "Stale input must fail");
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();
        var canceled = queue.Submit("r3", "d2", "wait", cancelled.Token);
        Check(!queue.TryBeginOnTurnThread("d2", out _), "Canceled queued input must not dispatch");
        Check(canceled.IsCanceled, "Pre-dispatch cancellation should resolve task");
        bool rejected = Task.Run(() => {
            try { queue.TryBeginOnTurnThread("d2", out _); return false; }
            catch (InvalidOperationException) { return true; }
        }).GetAwaiter().GetResult();
        Check(rejected, "Game operations must stay on their owning thread");
        var failed = queue.Submit("r4", "d2", "wait");
        Check(queue.TryBeginOnTurnThread("d2", out command), "Expected pending input");
        queue.FailAfterDispatchOnTurnThread(command!, new Exception("test failure"));
        Check(failed.IsFaulted, "Report failed dispatch");
        try { queue.Submit("r5", "d2", "wait"); throw new Exception("Faulted worker accepted input"); }
        catch (InvalidOperationException) { }
        queue.ReconcileOnTurnThread(new DecisionFrame("d3", 1, BoundaryKind.Command, "{}"));
        var recovered = queue.Submit("r6", "d3", "wait");
        Check(queue.TryBeginOnTurnThread("d3", out command), "Expected recovered input");
        queue.CompleteOnTurnThread(command!, new DecisionFrame("d4", 2, BoundaryKind.Command, "{}"));
        Check(recovered.IsCompletedSuccessfully, "Reconciliation should recover worker");
        Console.WriteLine("BridgeCore smoke tests passed.");
    }
}
