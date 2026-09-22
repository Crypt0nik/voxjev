/* Lanceur de voxjev.app.
 *
 * Il lance le GUI Python comme PROCESSUS ENFANT (et ne se transforme pas en Python) :
 * - macOS attribue micro / accessibilité / surveillance de l'entrée au processus responsable,
 *   c'est-à-dire voxjev.app (signé avec un certificat local stable) ;
 * - l'icône de la barre des menus appartient au processus Python, que macOS 26 sait classer dans
 *   Réglages › Barre des menus (« python3.12 ») : elle n'est donc plus masquée d'office ;
 * - rouvrir voxjev (Spotlight, Finder) envoie une notification au GUI, qui affiche son menu ;
 * - quand le GUI se termine, le lanceur aussi ; quand on quitte le lanceur, le GUI est arrêté.
 */
#import <Cocoa/Cocoa.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/stat.h>

#ifndef PROJECT_DIR
#error "compiler avec -DPROJECT_DIR=\"/chemin/du/projet\""
#endif

extern char **environ;
static pid_t child = 0;

@interface Launcher : NSObject <NSApplicationDelegate>
@end

@implementation Launcher
- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)flag {
    [[NSDistributedNotificationCenter defaultCenter] postNotificationName:@"local.voxjev.reopen"
                                                                   object:nil
                                                                 userInfo:nil
                                                       deliverImmediately:YES];
    return NO;
}
- (void)applicationWillTerminate:(NSNotification *)note {
    if (child > 0) kill(child, SIGTERM);
}
@end

int main(void) {
    @autoreleasepool {
        const char *home = getenv("HOME");
        char py[2048], src[2048], cache[2048], logdir[2048], logfile[2048];
        // Python embarqué lié au SDK récent (pymain.c, design Liquid Glass) ; repli : python du venv.
        // (Pas « .venv/bin/voxjev » : uv y écrit le script de la commande voxjev à chaque synchronisation.)
        snprintf(py, sizeof py, "%s/.venv/bin/voxjev-gui", PROJECT_DIR);
        if (access(py, X_OK) != 0) snprintf(py, sizeof py, "%s/.venv/bin/python", PROJECT_DIR);
        snprintf(src, sizeof src, "%s/src", PROJECT_DIR);
        snprintf(cache, sizeof cache, "%s/Library/Caches/voxjev/pycache", home);
        snprintf(logdir, sizeof logdir, "%s/Library/Logs/voxjev", home);
        snprintf(logfile, sizeof logfile, "%s/voxjev.log", logdir);
        mkdir(logdir, 0755);
        if (chdir(PROJECT_DIR) != 0) { perror("chdir"); return 1; }
        setenv("PYTHONPATH", src, 1);
        setenv("PYTHONPYCACHEPREFIX", cache, 1);
        setenv("PYTHONUNBUFFERED", "1", 1);
        setenv("VOXJEV_APP", "1", 1);

        posix_spawn_file_actions_t actions;
        posix_spawn_file_actions_init(&actions);
        posix_spawn_file_actions_addopen(&actions, 1, logfile, O_WRONLY | O_CREAT | O_APPEND, 0644);
        posix_spawn_file_actions_adddup2(&actions, 1, 2);
        char *const args[] = {py, "-m", "voxjev", "--gui", NULL};
        if (posix_spawn(&child, py, &actions, NULL, args, environ) != 0) {
            perror("posix_spawn");
            return 1;
        }
        posix_spawn_file_actions_destroy(&actions);

        NSApplication *app = [NSApplication sharedApplication];
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        Launcher *delegate = [Launcher new];
        app.delegate = delegate;

        // Le GUI s'est arrêté (menu › Quitter, erreur) : le lanceur se termine aussi.
        dispatch_source_t exitWatch = dispatch_source_create(DISPATCH_SOURCE_TYPE_PROC, (uintptr_t)child,
                                                             DISPATCH_PROC_EXIT, dispatch_get_main_queue());
        dispatch_source_set_event_handler(exitWatch, ^{
            int status;
            waitpid(child, &status, 0);
            child = 0;
            [NSApp terminate:nil];
        });
        dispatch_resume(exitWatch);
        signal(SIGTERM, SIG_IGN);
        dispatch_source_t term = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0,
                                                        dispatch_get_main_queue());
        dispatch_source_set_event_handler(term, ^{ [NSApp terminate:nil]; });
        dispatch_resume(term);
        [app run];
    }
    return 0;
}
