/* « voxjev-gui » : Python embarqué, compilé avec le SDK macOS courant.
 *
 * macOS 26 n'active le design Liquid Glass (contrôles en verre, grands arrondis, barres de
 * défilement fines) que pour les exécutables liés à son SDK. Le binaire python3.12 fourni par uv
 * l'est au SDK 14 : AppKit resterait en mode compatibilité. Cet exécutable, placé dans .venv/bin
 * (le venv est donc détecté normalement), lance simplement l'interpréteur.
 */
#include <Python.h>

int main(int argc, char **argv) {
    return Py_BytesMain(argc, argv);
}
