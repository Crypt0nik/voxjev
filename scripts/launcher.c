/* Lanceur de voxjev.app : démarre le GUI Python avec l'environnement du projet.
 * Un exécutable compilé (et non un script) fait de voxjev.app le « processus responsable » :
 * macOS attribue alors micro / accessibilité / surveillance de l'entrée à voxjev.app. */
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef PROJECT_DIR
#error "compiler avec -DPROJECT_DIR=\"/chemin/du/projet\""
#endif

int main(void) {
    const char *home = getenv("HOME");
    char py[2048], src[2048], cache[2048], logdir[2048], logfile[2048];
    snprintf(py, sizeof py, "%s/.venv/bin/python", PROJECT_DIR);
    snprintf(src, sizeof src, "%s/src", PROJECT_DIR);
    snprintf(cache, sizeof cache, "%s/Library/Caches/voxjev/pycache", home);
    snprintf(logdir, sizeof logdir, "%s/Library/Logs/voxjev", home);
    snprintf(logfile, sizeof logfile, "%s/voxjev.log", logdir);
    mkdir(logdir, 0755);
    int fd = open(logfile, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); }
    if (chdir(PROJECT_DIR) != 0) { perror("chdir"); return 1; }
    setenv("PYTHONPATH", src, 1);
    setenv("PYTHONPYCACHEPREFIX", cache, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("VOXJEV_APP", "1", 1);
    char *const args[] = {py, "-m", "voxjev", "--gui", NULL};
    execv(py, args);
    perror("execv");
    return 1;
}
