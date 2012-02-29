from buildbotcustom.try_parser import TryParser, processMessage
import unittest

###### TEST CASES #####

BUILDER_PRETTY_NAMES = {'macosx64':'OS X 10.6.2 try build', 'win32':'WINNT 5.2 try build', 'win32-debug':'WINNT 5.2 try leak test build', 'linux64':'Linux x86-64 try build', 'linux':'Linux try build', 'macosx64-debug':'OS X 10.6.2 try leak test build', 'linux64-debug':'Linux x86-64 try leak test build', 'linux-debug':'Linux try leak test build', 'macosx-debug':'OS X 10.5.2 try leak test build', 'android-r7':'Android R7 try build', 'maemo5-gtk':'Maemo 5 GTK try build'}
# TODO -- need to check on how to separate out the two win32 prettynames
TESTER_PRETTY_NAMES = {'macosx':['Rev3 MacOSX Leopard 10.5.8'], 'macosx64':['Rev3 MacOSX Snow Leopard 10.6.2', 'Rev3 MacOSX Leopard 10.5.8'], 'win32':['Rev3 WINNT 5.1', 'Rev3 WINNT 6.1'], 'linux-64':['Rev3 Fedora 12x64'], 'linux':['Rev3 Fedora 12']}
UNITTEST_PRETTY_NAMES = {'win32-debug':'WINNT 5.2 try debug test'}

TALOS_SUITES = ['tp4', 'chrome']
UNITTEST_SUITES = ['reftest', 'crashtest', 'mochitests-1/5', 'mochitests-3/5', 'mochitest-other']
MOBILE_UNITTEST_SUITES = ['reftest-1/3', 'reftest-3/3'] + UNITTEST_SUITES[1:]

VALID_UPN = ['WINNT 5.2 try debug test mochitests-1/5', 'WINNT 5.2 try debug test mochitests-3/5', 'WINNT 5.2 try debug test mochitest-other', 'WINNT 5.2 try debug test reftest', 'WINNT 5.2 try debug test crashtest']
VALID_REFTEST_NAMES = ['WINNT 5.2 try debug test reftest', 'WINNT 5.2 try debug test reftest-1/3', 'WINNT 5.2 try debug test reftest-3/3']
VALID_BUILDER_NAMES = ['OS X 10.6.2 try build', 'WINNT 5.2 try build', 'Linux x86-64 try build', 'Linux try build', 'OS X 10.5.2 try leak test build', 'OS X 10.6.2 try leak test build', 'WINNT 5.2 try leak test build', 'Linux x86-64 try leak test build', 'Linux try leak test build','Android R7 try build', 'Maemo 5 GTK try build']
VALID_TESTER_NAMES = ['Rev3 Fedora 12 try opt test mochitests-1/5', 'Rev3 Fedora 12 try opt test mochitest-other', 'Rev3 Fedora 12 try opt test crashtest', 'Rev3 Fedora 12 try debug test mochitests-1/5', 'Rev3 Fedora 12 try debug test mochitest-other', 'Rev3 WINNT 5.1 try opt test reftest', 'Rev3 WINNT 6.1 try opt test crashtest', 'Rev3 WINNT 6.1 try debug test crashtest', 'Rev3 WINNT 6.1 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitests-3/5', 'Rev3 MacOSX Leopard 10.5.8 try talos tp4', 'Rev3 WINNT 5.1 try talos chrome', 'Rev3 WINNT 6.1 try talos tp4', 'Rev3 WINNT 5.1 try talos tp4', 'Rev3 WINNT 6.1 try talos chrome']

class TestTryParser(unittest.TestCase):

    def test_BlankMessage(self):
        # Should get default set with blank input
        tm = ""
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        self.assertEqual(sorted(self.customBuilders),sorted(VALID_BUILDER_NAMES))

    def test_JunkMessageBuilders(self):
        # Should get default set with junk input
        tm = "try: junk"
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        self.assertEqual(sorted(self.customBuilders),sorted(VALID_BUILDER_NAMES))

    def test_JunkMessageTesters(self):
        # Should get default set with junk input to the test masters
        tm = "try: junk"
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 Fedora 12 try opt test mochitests-1/5', 'Rev3 Fedora 12 try opt test mochitest-other', 'Rev3 Fedora 12 try opt test crashtest', 'Rev3 Fedora 12 try debug test mochitests-1/5', 'Rev3 Fedora 12 try debug test mochitest-other', 'Rev3 WINNT 5.1 try opt test reftest', 'Rev3 WINNT 6.1 try opt test crashtest', 'Rev3 WINNT 6.1 try debug test crashtest', 'Rev3 WINNT 6.1 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitests-3/5']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_JunkBuildMessage(self):
        # Should get default set with junk input for --build
        tm = "try: -b k -p linux"
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['Linux try build','Linux try leak test build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_DebugOnlyBuild(self):
        tm = "try: -b d -p linux64,linux"
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['Linux x86-64 try leak test build', 'Linux try leak test build']
        self.assertEquals(sorted(self.customBuilders), sorted(builders))

    def test_OptOnlyBuild(self):
        tm = "try: -b o -p macosx64,linux"
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['OS X 10.6.2 try build', 'Linux try build']
        self.assertEquals(sorted(self.customBuilders), sorted(builders))

    def test_BothBuildTypes(self):
        # User can send 'do' or 'od' for both
        tm = ['try: -b od -p win32','try: -b do -p win32']
        for m in tm:
            self.customBuilders = TryParser(m, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
            builders = ['WINNT 5.2 try build', 'WINNT 5.2 try leak test build']
            self.assertEquals(sorted(self.customBuilders), sorted(builders))

    def test_SpecificPlatform(self):
        # Testing a specific platform, eg: mac only 
        # should specify macosx and macosx64 to get opt and debug
        tm = 'try: -b od -p macosx64,macosx'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['OS X 10.6.2 try build', 'OS X 10.6.2 try leak test build', 'OS X 10.5.2 try leak test build']
        self.assertEquals(sorted(self.customBuilders), sorted(builders))

    def test_AllPlatformsBoth(self):
        tm = 'try: -b od -p all'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['OS X 10.6.2 try build', 'WINNT 5.2 try build', 'Linux x86-64 try build', 'Linux try build', 'OS X 10.5.2 try leak test build', 'OS X 10.6.2 try leak test build', 'WINNT 5.2 try leak test build', 'Linux x86-64 try leak test build', 'Linux try leak test build', 'Android R7 try build', 'Maemo 5 GTK try build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_AllPlatformsOpt(self):
        tm = 'try: -b o -p all'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['OS X 10.6.2 try build', 'WINNT 5.2 try build', 'Linux x86-64 try build', 'Linux try build', 'Android R7 try build', 'Maemo 5 GTK try build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_AllPlatformsDebug(self):
        tm = 'try: -b d -p all'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES)
        builders = ['OS X 10.5.2 try leak test build', 'OS X 10.6.2 try leak test build', 'WINNT 5.2 try leak test build', 'Linux x86-64 try leak test build', 'Linux try leak test build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_AllOnTestMaster(self):
        tm = 'try: -a'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 Fedora 12 try opt test mochitests-1/5', 'Rev3 Fedora 12 try opt test mochitest-other', 'Rev3 Fedora 12 try opt test crashtest', 'Rev3 Fedora 12 try debug test mochitests-1/5', 'Rev3 Fedora 12 try debug test mochitest-other', 'Rev3 WINNT 5.1 try opt test reftest', 'Rev3 WINNT 6.1 try opt test crashtest', 'Rev3 WINNT 6.1 try debug test crashtest', 'Rev3 WINNT 6.1 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitests-3/5']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_MochitestAliasesOnBuilderMaster(self):
        tm = 'try: -b od -p win32 -u mochitests'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, BUILDER_PRETTY_NAMES, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try build', 'WINNT 5.2 try leak test build', 'WINNT 5.2 try debug test mochitest-other', 'WINNT 5.2 try debug test mochitests-3/5', 'WINNT 5.2 try debug test mochitests-1/5',]
        self.assertEqual(sorted(self.customBuilders),sorted(builders))
        tm = 'try: -b od -p win32 -u mochitest-o'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, BUILDER_PRETTY_NAMES, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try build', 'WINNT 5.2 try leak test build', 'WINNT 5.2 try debug test mochitest-other']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_MochitestAliasesOnTestMaster(self):
        tm = 'try: -b od -p all -u mochitests'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 Fedora 12 try opt test mochitests-1/5', 'Rev3 Fedora 12 try opt test mochitest-other', 'Rev3 Fedora 12 try debug test mochitests-1/5', 'Rev3 Fedora 12 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitests-3/5']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))
        tm = 'try: -b od -p win32 -u mochitest-o'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 WINNT 6.1 try debug test mochitest-other']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_MochitestAliasesOnTestMasterDebugOnly(self):
        tm = 'try: -b d -p all -u mochitests'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 Fedora 12 try debug test mochitests-1/5', 'Rev3 Fedora 12 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitest-other', 'Rev3 WINNT 6.1 try debug test mochitests-3/5']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_BuildMasterDebugWin32Tests(self):
        tm = 'try: -b d -p win32 -u mochitests'
        # test in the getBuilders (for local builder_master unittests)
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, {}, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try debug test mochitest-other', 'WINNT 5.2 try debug test mochitests-3/5', 'WINNT 5.2 try debug test mochitests-1/5']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_ReftestAliases(self):
        tm = 'try: -b d -p win32 -u reftests'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, {}, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try debug test reftest']
        self.assertEquals(sorted(self.customBuilders),sorted(builders))
        tm = 'try: -b d -p win32 -u reftest'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, {}, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try debug test reftest']
        self.assertEquals(sorted(self.customBuilders),sorted(builders))

    def test_ReftestMobileAliases(self):
        tm = 'try: -b d -p win32 -u reftests'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_REFTEST_NAMES, {}, UNITTEST_PRETTY_NAMES, MOBILE_UNITTEST_SUITES)
        builders = ['WINNT 5.2 try debug test reftest-1/3', 'WINNT 5.2 try debug test reftest-3/3']
        self.assertEquals(sorted(self.customBuilders),sorted(builders))
        tm = 'try: -b d -p win32 -u reftest'
        builders = ['WINNT 5.2 try debug test reftest-1/3', 'WINNT 5.2 try debug test reftest-3/3']
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_REFTEST_NAMES, {}, UNITTEST_PRETTY_NAMES, MOBILE_UNITTEST_SUITES)
        self.assertEquals(sorted(self.customBuilders),sorted(builders))

    def test_SelectTests(self):
        tm = 'try: -b od -p win32 -u crashtest,mochitest-other'
        # test in the getBuilders (for local builder_master unittests)
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, BUILDER_PRETTY_NAMES, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try build', 'WINNT 5.2 try leak test build', 'WINNT 5.2 try debug test crashtest', 'WINNT 5.2 try debug test mochitest-other']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))
        # test in the getTestBuilders (for local builder_master unittests)
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 WINNT 6.1 try opt test crashtest', 'Rev3 WINNT 6.1 try debug test crashtest', 'Rev3 WINNT 6.1 try debug test mochitest-other']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_NoTests(self):
        tm = 'try: -b od -p linux,win32 -u none'
        # test in getBuilders
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['Linux try build', 'Linux try leak test build', 'WINNT 5.2 try build', 'WINNT 5.2 try leak test build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))
        # test in getTestBuilders
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = []
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_AllTalos(self):
        # should get all unittests too since that's the default set
        tm = 'try: -b od -t all'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES, TALOS_SUITES)
        self.assertEqual(sorted(self.customBuilders),sorted(VALID_TESTER_NAMES))

    def test_SelecTalos(self):
        tm = 'try: -b od -p win32 -t tp4'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, None, TALOS_SUITES)
        builders = ['Rev3 WINNT 6.1 try talos tp4', 'Rev3 WINNT 5.1 try talos tp4']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_SelecTalosWithNoTalosPlatforms(self):
        tm = 'try: -b od -p win32,android-r7 -t tp4'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, None, TALOS_SUITES)
        builders = ['Rev3 WINNT 6.1 try talos tp4', 'Rev3 WINNT 5.1 try talos tp4']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_NoTalos(self):
        tm = 'try: -b od -p linux,win32 -t none'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, TESTER_PRETTY_NAMES, None, None, TALOS_SUITES)
        builders = []
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_DebugWin32OnTestMaster(self):
        tm = 'try: -b do -p win32 -u crashtest'
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES,None, UNITTEST_SUITES)
        builders = ['Rev3 WINNT 6.1 try debug test crashtest', 'Rev3 WINNT 6.1 try opt test crashtest']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_HiddenCharactersAndOldSyntax(self):
        tm = 'attributes\ntry: -b o -p linux64 -m none -u reftest -t none'
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Linux x86-64 try build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def test_NoBuildTypeSelected(self):
        tm = 'try: -m none -u crashtest -p win32' 
        # should get both build types for the selected platform
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES, BUILDER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try build', 'WINNT 5.2 try leak test build']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

        # should get debug win32 in the builder_master test builders
        self.customBuilders = TryParser(tm, VALID_BUILDER_NAMES+VALID_UPN, {}, UNITTEST_PRETTY_NAMES, UNITTEST_SUITES)
        builders = ['WINNT 5.2 try debug test crashtest']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

        # should get both build types in test_builders
        self.customBuilders = TryParser(tm, VALID_TESTER_NAMES, TESTER_PRETTY_NAMES, None, UNITTEST_SUITES)
        builders = ['Rev3 WINNT 6.1 try opt test crashtest', 'Rev3 WINNT 6.1 try debug test crashtest']
        self.assertEqual(sorted(self.customBuilders),sorted(builders))

    def _testNewLineProcessMessage(self, message, value=None):
        if not value:
            value = ['-a', '-b', '-c']
        self.assertEqual(processMessage(message), value)

    def test_SingleLine(self):
        self._testNewLineProcessMessage("""try: -a -b -c""")

    def test_SingleLineSpace(self):
        self._testNewLineProcessMessage("""try: -a -b -c """)

    def test_CommentSingleLine(self):
        self._testNewLineProcessMessage("""blah blah try: -a -b -c""")

    def test_CommentSingleLineSpace(self):
        self._testNewLineProcessMessage("""blah blah try: -a -b -c """)

    def test_FirstLineNewLine(self):
        self._testNewLineProcessMessage("""try: -a -b -c
some other comment
lines
blah""")

    def test_FirstLineSpaceNewLine(self):
        self._testNewLineProcessMessage("""try: -a -b -c 
some other comment
lines
blah""")

    def test_CommentFirstLineNewLine(self):
        self._testNewLineProcessMessage("""blah blah try: -a -b -c
some other comment
lines
blah""")

    def test_CommentFirstLineSpaceNewLine(self):
        self._testNewLineProcessMessage("""blah blah try: -a -b -c 
some other comment
lines
blah""")

    def test_MiddleLineNewLine(self):
        self._testNewLineProcessMessage("""blah blah
try: -a -b -c
some other comment
lines
blah""")

    def test_MiddleLineSpaceNewLine(self):
        self._testNewLineProcessMessage("""blah blah
try: -a -b -c 
some other comment
lines
blah""")

    def test_CommentMiddleLineNewLine(self):
        self._testNewLineProcessMessage("""blah blah
blah blah try: -a -b -c
some other comment
lines
blah""")

    def test_CommentMiddleLineSpaceNewLine(self):
        self._testNewLineProcessMessage("""blah blah
blah blah try: -a -b -c 
some other comment
lines
blah""")

    def test_LastLine(self):
        self._testNewLineProcessMessage("""blah blah
some other comment
lines
try: -a -b -c""")

    def test_LastLineSpace(self):
        self._testNewLineProcessMessage("""blah blah
some other comment
lines
try: -a -b -c """)

    def test_CommentLastLine(self):
        self._testNewLineProcessMessage("""blah blah
some other comment
lines
blah blah try: -a -b -c""")

    def test_CommentLastLineSpace(self):
        self._testNewLineProcessMessage("""blah blah
some other comment
lines
blah blah try: -a -b -c """)

    def test_DuplicateTryLines(self):
        self._testNewLineProcessMessage("""try: -a -b -c
try: -this -should -be -ignored""")

    def test_IgnoreTryColonNoSpace(self):
        self._testNewLineProcessMessage("""Should ignore this try:
try: -a -b -c""")


if __name__ == '__main__':
    unittest.main()
