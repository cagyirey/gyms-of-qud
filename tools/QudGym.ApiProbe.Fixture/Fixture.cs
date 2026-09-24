// Synthetic metadata fixture. This is NOT Qud code or evidence of its API.
using System.Runtime.CompilerServices;

namespace XRL {
    public interface IPlayerMutator { void mutate(World.GameObject player); }
    [AttributeUsage(AttributeTargets.Class)]
    public sealed class PlayerMutatorAttribute : Attribute { }
    internal static class MustNeverRun {
        [ModuleInitializer]
        public static void Initialize() => throw new InvalidOperationException("Probe executed fixture code!");
    }
}
namespace XRL.World {
    public class EndTurnEvent { public static int ID = 7; }
    public class GameObject {
        public string DisplayName => throw new InvalidOperationException("Getter executed!");
        public int Stat(string name) => throw new InvalidOperationException("Method executed!");
        public T? GetPart<T>() where T : class => null;
        public void OmittedSecret() { }
    }
    public abstract class IPart {
        public GameObject? ParentObject;
        public virtual bool WantEvent(int id, int cascade) => false;
        public virtual bool HandleEvent(EndTurnEvent e) => false;
    }
}
