"""An i18n linting tool for exercises.

Catches common i18n problems in exercises and recommends solutions to
fix them. Wherever possible the script can also automatically fix the
problems with limited user interaction.

By default the script acts as a linter outputting errors that must be
corrected by hand and a list of any errors that could be fixed
automatically (which is still considered to be a linting failure).

If run with the --fix flag then the script will automatically fix the
files in place wherever possible. The user will be prompted to clear
up any ambiguities as well.
"""

import argparse
import copy
import re
import sys

import extract_strings


# Should the user be prompted when a case is ambiguous?
SHOW_PROMPT = True

# Used to cache the results from the user prompt pluralization methods
_PLURAL_FORMS = {
    # Hardcode a few common pluralizations in
    '': 's',
    'is': 'are',
    'was': 'were'
}
_PLURAL_NUM_POS = {}
_IS_PLURAL_NUM = {}

# A list of all the built-in functions which are sometimes pluralized
# We effectively treat these as strings since their pluralization is
# already taken care of in word-problems.js
_functions = ['deskItem', 'exam', 'item', 'storeItem', 'crop', 'distance',
    'exercise', 'pizza', 'animal', 'fruit', 'group', 'clothing']

# In an ambiguous case the presence of these strings tend to indicate
# what the variable holds
_string_vars = ['TEXT', 'TYPE', 'UNIT', 'LOCATION']
_num_vars = ['NUM', 'AMOUNT', 'TOTAL']

# Helper regexs for determining if something looks like a string
# or a function call
_STRING_RE = re.compile(r'^\s*["\'](.*?)["\']\s*$')
_FUNCTION_RE = re.compile(r'^\s*(\w+)\(.*\)\s*$')


def main():
    """Handle running this program from the command-line."""
    # Handle parsing the program arguments
    arg_parser = argparse.ArgumentParser(
        description='Extract translatable strings from HTML exercise files.')
    arg_parser.add_argument('html_files', nargs='+',
        help='The HTML exercise files to extract strings from.')
    arg_parser.add_argument('--quiet', '-q', action='store_true',
        help='Do not emit status to stderr on successful runs.')
    arg_parser.add_argument('--fix', action='store_true',
        help='Automatically fix some i18n issues in the input files.')

    args = arg_parser.parse_args()

    # Don't prompt the user if we're not fixing the results
    if not args.fix:
        global SHOW_PROMPT
        SHOW_PROMPT = False

    # Keep track of how many errors and fixes occur and in how many files
    total_errors = 0
    total_error_files = 0
    total_fixes = 0
    total_fix_files = 0

    # Go through all the fileanmes provided
    for filename in args.html_files:
        # Lint the file, returns a list of error messages and
        # a count of the number of fixes that were automatically
        # applied (depending upon --fix)
        (errors, num_fixes) = lint_file(filename, args.fix, not args.quiet)

        # Keep track of how many files have been fixed
        if num_fixes:
            # Keep track of how many fixes have been done
            total_fixes += num_fixes
            total_fix_files += 1

        if errors:
            num_errors = len(errors)

            # Keep track of how many errors have occurred
            total_errors += num_errors

            # Keep track of how many files have errors
            total_error_files += 1

            # Print out a notice indicating that an error occurred
            # in that file.
            print >>sys.stderr, ('%s error%s: %s.' % (
                num_errors, "" if num_errors == 1 else "s", filename))

            # Print out all the error messages
            for error_msg in errors:
                print >>sys.stderr, error_msg

        # If nodes were automatically fixed output the result
        if not args.quiet and args.fix and num_fixes:
            print >>sys.stderr, ('%s node%s have been fixed in %s.' % (
                num_fixes, "" if num_fixes == 1 else "s", filename))

    # Output the results of having fixed the files automatically
    if not args.quiet and args.fix:
        print >>sys.stderr, ('%s nodes fixed in %s file%s.' % (
            total_fixes, total_fix_files, "" if total_fix_files == 1 else "s"))

    # Output a total number of errors that have occurred
    if total_errors:
        print >>sys.stderr, ('%s error%s detected in %s file%s.' % (
            total_errors, "" if total_errors == 1 else "s",
            total_error_files, "" if total_error_files == 1 else "s"))

    sys.exit(min(total_errors, 127))


def lint_file(filename, apply_fix, verbose):
    """Fix a single HTML exercise repairing invalid nodes.

    Returns an array of node tuples which cannot be fixed automatically and
    must be fixed by hand. Nodes that can be fixed automatically are fixed
    and the file is updated, if apply_fix is set to True.

    Arguments:
        - filename: A string filename to parse
        - apply_fix: If True, then filename is replaced with new contents,
          which is the fixed version of the old contents.
        - verbose: If there should be any output
    Returns:
        - A tuple (errors, num_nodes_changed) which contains `errors`,
          which is a list holding strings describing errors found in the file
          and `num_nodes_changed` which is a number counting how many nodes
          were changed by the script (or could've been changed, if the
          apply_fix flag is set to False).
    """
    # A list of all the errors that occurred
    errors = []

    # Keep track of how many nodes have changed in the document
    # (Used to figure out if we need to write out a new version of the file)
    nodes_changed = 0

    # The filters through which the files should be passed and in which order
    filters = [PronounFilter, TernaryFilter, AlwaysPluralFilter, PluralFilter]

    # Collect all the i18n-able nodes out of file
    nodes = extract_strings.extract_nodes(filename)

    # Root HTML Tree
    root_tree = nodes[0].getroottree() if nodes else None

    # Do a first pass linting against the file. This looks for rejected nodes
    # inside of extracted strings. For example, if a graphie element is in
    # an extracted string that is an error and the code needs to be fixed.

    # Construct an XPath expression for finding rejected nodes
    lint_expr = "|".join([".//%s" % name for name in
        extract_strings.REJECT_NODES])

    for node in nodes:
        # If we're linting the file and the string doesn't contain any
        # rejected nodes then we just ignore it
        lint_nodes = node.xpath(lint_expr)

        for lint_node in lint_nodes:
            errors.append("Contains invalid node:\n%s\nInvalid node:\n%s" % (
                extract_strings.get_outerhtml(node),
                extract_strings.get_outerhtml(lint_node)))

    # And now we run the nodes through all of our fixable filters. These
    # filters detect nodes that can be automatically fixed (and fixes them
    # if the apply_fix flag is set to True). It also detects nodes that
    # should be fixed but need some manual adjustment before they can be
    # automatically fixed. Those come up as errors.

    # Process the file with each filter in series
    for filter_class in filters:
        # Instantiate the filter
        filter = filter_class()

        # Have it process all the nodes in the document
        (new_nodes, new_errors, new_nodes_changed) = filter.process(nodes)

        # It's possible that the nodes will change, be replaced, or be inserted
        # during the processing of the filter. To avoid having to re-load and
        # parse the file a second time we build a list of nodes dynamically
        # from the filtered results.
        nodes = new_nodes

        # Add any errors onto the full list of errors
        errors += new_errors

        # Keep track of how many nodes have changed
        # (or would have changed, if apply_fix is False)
        nodes_changed += new_nodes_changed

    if nodes_changed:
        # If any nodes have changed and we want to apply the fixes
        if apply_fix:
            # Then write out the modified file
            with open(filename, 'w') as f:
                f.write(extract_strings.get_page_html(root_tree))
        else:
            # Consider it to be an error when there are nodes that need
            # fixing and we haven't run with --fix
            errors.append(('%s node%s need to be fixed. '
                'Re-run with --fix to automatically fix them.' % (
                    nodes_changed, "" if nodes_changed == 1 else "s")))

    return (errors, nodes_changed)


class BaseFilter(object):
    """A base filter, replaces nodes and <var> elements.
    
    Sub-classes must define the following:
     - xpath: A string that holds the XPath expression for finding nodes.
     - filter_var: A method for processing a single fixable <var>.
     - get_match: A method for determining if a <var> matches 
    """
    def __init__(self):
        """Intitialize and keep track of nodes_changed and errors."""
        self.nodes_changed = 0
        self.errors = []

    def process(self, nodes):
        """Process all the nodes in the document.

        Returns a tuple of the resulting nodes, a list of error strings, and
        a number indictating how many nodes have changed.
        """
        # It's possible that the nodes will change, be replaced, or be inserted
        # during the processing of the filter. To avoid having to re-load and
        # parse the file a second time we build a list of nodes dynamically
        # from the filtered results.
        new_nodes = []

        for node in nodes:
            # Process a single node
            result = self.process_node(node)

            # It's possible that multiple nodes have been returned, if that's
            # the case then we extend the list
            if isinstance(result, (tuple, list)):
                new_nodes.extend(result)
            # Otherwise we just append the node to the list
            else:
                new_nodes.append(result)

        return (new_nodes, self.errors, self.nodes_changed)

    def process_node(self, orig_node):
        """Process a single node.

        Bail if the node doesn't contain any elements that may need
        fixing. (We discard the results of running this against the
        original node as we really want the result from the cloned
        node.) Unfortunately cloning the nodes is a more-expensive
        operation than running the XPath expression so we do this
        first to offset the expense.

        Returns the existing node or the modified node, if need be.
        """
        if not self.find_fixable_vars(orig_node):
            return orig_node

        # Copy the existing node and make a new one, if need be
        node = self.copy_node(orig_node)

        # A collection of all the <var>s under this node that might need fixing
        self.fixable_vars = self.find_fixable_vars(node)

        # Process the fixable vars in the node
        if not self.process_vars(self.fixable_vars):
            return orig_node

        # Replace orig_node with the new node we've generated, in the html tree
        self.replace_node(orig_node, node)

        return node

    def find_fixable_vars(self, node):
        """Locate all the <var> elements that need fixing in a node.

        Returns a list of nodes.
        """
        # Construct an XPath expression for finding nodes to fix
        fix_expr = '|'.join(['.//%s[%s]' % (name, self.xpath)
            for name in extract_strings.INLINE_SCRIPT_NODES])

        return node.xpath(fix_expr)

    def copy_node(self, orig_node):
        """Create a copy of the node for further processing.
        
        Returns a copied version of the node.
        """
        # We copy the node to make sure we don't unintentionally modify
        # the original node.
        return copy.deepcopy(orig_node)

    def replace_node(self, orig_node, node):
        """Replace a node if we've generated a new node."""
        # We just replace the node with the newly-cloned node
        if orig_node != node:
            orig_node.getparent().replace(orig_node, node)

    def process_vars(self, fixable_vars):
        """Process all the <var> elements in a node.

        Returns True if no errors were found.
        """
        # Loop through the fixable var nodes
        for var_node in fixable_vars:
            # Extract parts of the code element's inner contents for
            # further processing.
            match = self.get_match(var_node)

            if match:
                # Process the fixable var
                self.filter_var(match, var_node)

                # Keep a tally of nodes that've been changed
                self.nodes_changed += 1

        return True

    def get_match(self, var_node):
        raise NotImplementedError('Subclasses must define this')

    def filter_var(self, var_node):
        raise NotImplementedError('Subclasses must define this')


class IfElseFilter(BaseFilter):
    """A filter for handling the generation of data-if/data-else nodes.

    This builds off of BaseFilter and modifies it in some critical ways:
     - The contents of <var> elements are inspected to extract unique keys
       upon which a data-if condition should be built.
     - If more than one key is detected then an error is generated.
     - A new, cloned, node is generated to hold the contents of the data-else
       element and its contents.
     - If the node already has a data-if or data-else attribute then new inner
       nodes are generated instead.

    Sub-classes need to implement:
     - extract_key: A method for pulling a unique key from a match.
     - get_condition: A method that returns the condition to add to the node.
    """
    def process_node(self, orig_node):
        """Process a single node.

        Generates a clone of the node to be used to hold the data-else portion
        of the result. Also checks the keys detected to see if there are any
        problems. Finally, adds in the data-else and injects the cloned node.

        Returns the existing node or the modified node, if need be. Could also
        return a tuple of nodes
        """
        # Create a cloned copy of the node, we're going to need this as
        # the fixer will likely need to generate a second copy of the
        # original node (for the 'data-else') but slightly modified.
        self.cloned_node = copy.deepcopy(orig_node)
        self.cloned_node.tail = ''

        # The vars that might need fixing under the cloned element
        self.cloned_vars = self.find_fixable_vars(self.cloned_node)

        # Process the node using the BaseFilter
        node = super(IfElseFilter, self).process_node(orig_node)

        # There's a reason for ignoring the node so we just end early
        if node is orig_node:
            return orig_node

        # If we've located more than one key then we need to fix the
        # strings by hand.
        if len(self.match_keys) > 1:
            self.errors.append("Contains too many different keys (%s):\n%s" % (
                ", ".join(self.match_keys),
                extract_strings.get_outerhtml(orig_node)))
            return orig_node

        # Only continue if there are keys to process
        if self.match_keys:
            # Get the one remaining key
            key = self.match_keys[0]

            # Add an if condition to the node
            node.set('data-if', self.get_condition(key))

            # Add the data-else attribute to the cloned node
            self.cloned_node.set('data-else', '')

            # And insert it after the original node
            node.addnext(self.cloned_node)

            # Keep track of nodes that've been changed
            self.nodes_changed += 1

            # Return both nodes for futher processing
            return (node, self.cloned_node)

        return node

    def _get_cloned_var(self, var_node):
        """Given a <var> node return the equivalent node from the cloned node.

        This is used to make it easy to work with the two sets of nodes
        simultaneously.
        """
        return self.cloned_vars[self.fixable_vars.index(var_node)]

    def process_vars(self, fixable_vars):
        """Extract the keys from all the <var>s in the node."""
        # Some nodes will have a unique 'key' which will be used as a
        # lookup. For example in the following nodes:
        #    <p><var>He(1)</var> threw a ball to <var>his(1)</var>
        #       friend.</p>
        #    <p><var>He(1)</var> threw a ball to <var>him(2)</var>.</p>
        # The first string has one key '1' used twice, whereas the second
        # string has two keys '1' and '2'. We keep track of this because
        # we need to use this key to generate the replacement string and
        # also to make sure that we don't attempt to fix a string that has
        # more than one key in it. For example the first string becomes:
        #    <p data-if="isMale(1)">He threw a ball to his friend.</p>
        #    <p data-else>She threw a ball to her friend.</p>
        # And the second one is not possible to automatically fixable
        # because it has more than one key.
        match_keys = set()

        for var_node in fixable_vars:
            # Extract parts of the code element's inner contents for
            # further processing.
            match = self.get_match(var_node)

            if match:
                # Extract the key from the string (if it exists)
                key = self.extract_key(match)

                # If a key was extracted then add it to the set
                if key:
                    match_keys.add(key)

        self.match_keys = list(match_keys)

        # Run the BaseFilter process_vars
        return super(IfElseFilter, self).process_vars(fixable_vars)

    def replace_node(self, orig_node, node):
        """Replace the node only if it doesn't have a data-if/data-else.

        This is because nodes that have a data-if or data-else are left
        in-place and new wrappers were generated and injected in copy_node.
        """
        if not orig_node.get('data-if') and not orig_node.get('data-else'):
            return super(IfElseFilter, self).replace_node(orig_node, node)

    def copy_node(self, orig_node):
        """Copy the node only if it doesn't have a data-if/data-else.
        
        We leave nodes that have a data-if or data-else in-place and new
        <span> wrappers are generated and injected instead.
        """
        if orig_node.get('data-if') or orig_node.get('data-else'):
            # We clone the node to make sure we don't unintentionally modify
            # the original node.
            node = copy.deepcopy(orig_node)
            node.tail = ''

            # Change the tag names to just be a boring 'span'
            node.tag = self.cloned_node.tag = 'span'

            # Remove all existing attributes on both the original and the
            # cloned node
            for attr in node.attrib:
                node.attrib.pop(attr)
            for attr in self.cloned_node.attrib:
                self.cloned_node.attrib.pop(attr)

            # Set the data-unwrap attribute to get the exercise framework
            # to automatically remove the <span> wrapper that we added
            node.set('data-unwrap', '')
            self.cloned_node.set('data-unwrap', '')

            # Remove all child nodes within the original element
            for child_node in orig_node.iterchildren():
                orig_node.remove(child_node)

            # Clear any remaining text
            orig_node.text = ''

            # And insert the newly-created node into position
            orig_node.append(node)

            # Note: We no longer need to do replace_node as we've effectively
            # inserted the nodes that we're operating against into the tree.

            return node

        # Run the BaseFilter copy_node
        return super(IfElseFilter, self).copy_node(orig_node)

    def get_condition(self, key):
        raise NotImplementedError('Subclasses must define this')

    def extract_key(self, match):
        raise NotImplementedError('Subclasses must define this')


class PronounFilter(IfElseFilter):
    """Repairs usage of he()/He()/his()/His() in exercise files.
    Used by lint_file, automatically converts these methods into
    a more translatable form.

    For example given the following string:
        <p><var>He(1)</var> threw it to <var>his(1)</var> friend.</p>

    This filter will convert it into the following two nodes:
        <p data-if="isMale(1)">He threw it to his friend.</p>
        <p data-else>She threw it to her friend.</p>

    Creating two distinct strings for each gender (greatly simplifying
    the translation process).
    """
    _pronouns = ['he', 'He', 'his', 'His']
    _pronoun_map = {'he': 'she', 'He': 'She', 'his': 'her', 'His': 'Her'}
    _pronoun_condition = 'isMale(%s)'
    # Matches he|his(...)
    _regex = re.compile(r'^\s*(he|his)\(\s*(.*?)\s*\)\s*$', re.I)

    xpath = ' or '.join(['contains(text(),"%s(")' % pronoun
        for pronoun in _pronouns])

    def get_match(self, fix_node):
        """Return a match of a string that matches he|his(...)"""
        return self._regex.match(fix_node.text)

    def extract_key(self, match):
        """From the match return the key of the string.

        For example with: he(1), '1' would be returned.
        """
        return match.group(2)

    def filter_var(self, match, var_node):
        """Replace the fixable node with the correct gender string.

        For example: <var>He(1)</var> will be 'He' and 'She' in the
        original and cloned nodes.
        """
        extract_strings.replace_node(var_node, match.group(1))
        extract_strings.replace_node(self._get_cloned_var(var_node),
            self._pronoun_map[match.group(1)])

    def get_condition(self, key):
        """Generates a data-if condition to handle the gender toggle.

        This will turn a string like <p><var>He(1)</var> ran.</p> into:
            <p data-if="isMale(1)">He ran.</p><p data-else>She ran.</p>
        """
        return self._pronoun_condition % key


class AlwaysPluralFilter(BaseFilter):
    """Fix usage of plural() in exercises when the result is always plural.

    For example the string <var>plural(distance(1))</var> will always return
    the plural form of distance(1). We rewrite it to use a new method named
    `plural_form` which will always return the plural form of that word.

    There does exist some ambiguous cases and for those we need to prompt the
    user to determine if we're dealing with a string or a number. For example
    with the case: <var>plural(NUM)</var> the plural() method will return
    an 's' if the number is greater than 1 or an empty string if it is 1.

    Additionally sometimes the case of <var>plural("word")</var> was used,
    which is silly, so we just replace it with the text "words".
    """
    _empty_str_fn = '%s("", %s)'
    # Map old function name to new function name
    _function_map = {
        'plural': 'plural_form(%s)',
        'pluralTex': 'plural_form(%s)'
    }
    # Matches plural(...)
    _regex = re.compile(r'^\s*(plural|pluralTex)'
        r'\(\s*((?:[^,]+|\([^\)]*\))*)\s*\)\s*$', re.I)

    xpath = ' or '.join(['contains(text(),"%s(")' % method
        for method in _function_map.keys()])

    def get_match(self, fix_node):
        """Return a match of a string that matches plural(...)"""
        return self._regex.match(fix_node.text)

    def filter_var(self, match, var_node):
        """Replace the <var> with the correct contents.

        This depends upon the contents of the plural() string.

        When the argument is a string literal. For example:
            <var>plural("string")</var>
        Will produce:
            strings

        When the variable holds a string. For example:
            <var>plural(UNIT_TEXT)</var>
        Will produce:
            <var>plural_form(UNIT_TEXT)</var>

        When the variable holds a number. For example:
            <var>plural(NUM)</var>
        Will produce:
            <var>plural_form("", NUM)</var>
        """
        # Handle the case where a raw string is used
        str_match = _STRING_RE.match(match.group(2))
        if str_match:
            # In this case just convert it directly to its plural form
            # We do this by prompting the user for help translating to the
            # correct plural form.
            extract_strings.replace_node(var_node,
                get_plural_form(str_match.group(1)))
        # If the argument is a number
        elif get_is_plural_num(match):
            # Then we need to rewrite the function call so that it'll
            # be transformed into plural("", NUM), which will then be
            # converted into its correct form via the PluralFilter
            var_node.text = self._empty_str_fn % (match.group(1).strip(),
                match.group(2).strip())
        else:
            # Otherwise we need to wrap the variable (or function call) in
            # a call to plural_form() which will attempt to return the
            # plural form of that string.
            var_node.text = (self._function_map[match.group(1)] %
                match.group(2).strip())


class PluralFilter(IfElseFilter):
    """Fix usage of plural() in exercises.

    This filter fixes a number of different issues relating to the usage of
    plural() in exercises. An interactive prompt is used to clear up any
    information that can't be resolved automatically.

    To start with it fixes the usage of two plural signatures.
    The signature: <var>plural(STRING, NUM)</var> is pretty much left intact.

    For example given the following string:
        <p>I ran <var>NUM</var> <var>plural(distance(1), NUM)</var>.</p>
    It will generate the following two strings:
        <p data-if="isSingular(NUM)">I ran <var>NUM</var>
            <var>distance(1)</var>.</p>
        <p data-else>I ran <var>NUM</var>
            <var>plural_form(distance(1), NUM)</var>.</p>

    And given the following string:
        <p>I ran <var>NUM</var> <var>plural(NUM, distance(1))</var>.</p>
    It will generate the following two strings:
        <p data-if="isSingular(NUM)">I ran <var>NUM</var>
            <var>distance(1)</var>.</p>
        <p data-else>I ran <var>NUM</var>
            <var>plural_form(distance(1), NUM)</var>.</p>

    (Note that the signature `plural(NUM, STRING)` outputs the number in
    addition to the string itself.)

    The tricky part about this ambiguous method signature is in figuring
    out the different cases and how to resolve them. Let's step through each
    possible case to show you how it's done.

    The easiest case is when one of the arguments to the plural() function
    is a string.

    For example given the following string:
        <p>I ran <var>plural(NUM, "mile")</var>.</p>
    It will generate the following two strings:
        <p data-if="isSingular(NUM)">I ran 1 mile.</p>
        <p data-else>I ran <var>NUM</var> miles.</p>

    The pluralization of the static string is done by prompting the user
    for the correct plural form of the word.

    The second most common case is in using one of the built-in string methods
    such as `distance(POS)`, `item(POS)`, or `clothing(POS)`. We look for all
    of these possible signatures and if one exists then we assume that that
    is the string argument. The output will be the same as one of the above
    examples.

    The final case is when the arguments are truly ambiguous: When there is
    no obvious way to detect if one argument is a string and one is a number.
    We fix this by prompting the user for help in determining which argument
    holds the number. With this information we can then easily resolve the
    output (in a form similar to the handling of the built-in methods).
    """
    # Map old function name to new function name
    _function_map = {
        'plural': 'plural_form(%s, %s)',
        'pluralTex': 'plural_form(%s, %s)'
    }
    _ngetpos_condition = 'isSingular(%s)'
    # See if it matches the form plural|pluralTex(..., ...)
    _regex = re.compile(r'^\s*(plural|pluralTex)'
        r'\(\s*((?:[^,(]+|\(.+?\))*),\s*((?:[^,(]+|\(.+?\))*)\s*\)\s*$', re.I)

    xpath = ' or '.join(['contains(text(),"%s(")' % method
        for method in _function_map.keys()])

    def get_match(self, fix_node):
        """Return a match of a string that matches plural(...)"""
        # See if it matches the form plural|pluralTex(..., ...)
        return self._regex.match(fix_node.text)

    def extract_key(self, match):
        """Extract a unique identifier upon which to toggle the plural form.

        For the case of calls to plural() we need to determine which argument
        is the number as that's the value upon which we must toggle.
        """
        # Determine the position of the number argument and extract it
        return match.group(get_plural_num_pos(match) + 1).strip()

    def filter_var(self, match, var_node):
        """Replace the <var>s with the correct plural() contents.

        If the first argument to plural() is a string:
        This means that the plural() function is expected to output just
        the pluralized form of the string itself. We take this and turn
        it into two blocks toggled with an if/else and with the string
        hardcoded into it. For example:
           <p>I have <var>plural("a cat", NUM)</var>.</p>
        Would then become (after user prompting):
           <p data-if="isSingular(NUM)">I have a cat.</p>
           <p data-else>I have many cats.</p>

        If the second argument to plural() is a string:
        This means that the plural() function is expected to output a
        number and the pluralized form of the string. We take this and turn
        it into two blocks toggled with an if/else and with the number
        variable and the string hardcoded into it. For example:
           <p>I have <var>plural(NUM, "cat")</var>.</p>
        Would then become (after user prompting):
           <p data-if="isSingular(NUM)">I have <var>NUM</var> cat.</p>
           <p data-else>I have <var>NUM</var> cats.</p>

        Otherwise both of the results are variables or function calls:
        This means that we need to insert the variables directly, for example:
           <p>I have <var>plural(NUM, item(1))</var>.</p>
        Would then become (after user prompting):
           <p data-if="isSingular(NUM)">I have <var>NUM</var>
               <var>item(1)</var>.</p>
           <p data-else>I have <var>NUM</var>
               <var>plural_form(item(1), NUM)</var>.</p>
        To do this we need to determine which argument is the number variable
        and then change the output depending upon it (because of the silly
        plural() function argument order).
        """
        cloned_var = self._get_cloned_var(var_node)

        first_str_match = _STRING_RE.match(match.group(2))
        second_str_match = _STRING_RE.match(match.group(3))

        # If the first argument is a string:
        if first_str_match:
            # Get the word out of the string
            word = first_str_match.group(1).strip()

            # Replace the first node with just the word
            extract_strings.replace_node(var_node, word)

            # Replace the cloned node with the plural form of the word
            extract_strings.replace_node(cloned_var,
                get_plural_form(word))

        # If the second argument is a string
        elif second_str_match:
            # Get the word out of the string
            word = second_str_match.group(1).strip()

            # Have the <var> output just the number
            var_node.text = cloned_var.text = match.group(2).strip()

            # Insert the word after the singular <var>
            var_node.tail = ' ' + word + (var_node.tail or '')

            # Insert a space and the plural form of the word after the variable
            cloned_var.tail = (' ' + get_plural_form(word) +
                (cloned_var.tail or ''))

        # Otherwise both of the results are variables or function calls.
        else:
            # The string which will be used to wrap the output variable
            pluralize = self._function_map[match.group(1)]

            # Get the position of the number variable from the match
            plural_num_pos = get_plural_num_pos(match)

            # Number is in the first position, this results in the output:
            # "NUM STRING". This signature is deprecated so we're going to
            # convert it into a more translatable form.
            if plural_num_pos == 1:
                # We're going to turn the following:
                #   <var>plural(NUM, STRING)</var>
                # Into the following for the singular and plural cases:
                #   <var>NUM</var> <var>STRING</var>
                #   <var>NUM</var> <var>plural_form(STRING)</var>

                # We start by replacing the contents of the node with just the
                # STRING var text resulting in: <var>NUM_VAR</var>
                var_node.text = cloned_var.text = match.group(2).strip()

                # We want to generate HTML that looks like this:
                # <var>NUM_VAR</var> <var>STRING_VAR</var>
                # <var>NUM_VAR</var> <var>plural_form(STRING_VAR)</var>

                # We need to insert a new <var> element after the existing one
                singular_var_node = var_node.makeelement('var')
                plural_var_node = cloned_var.makeelement('var')

                # In the singular case we just output <var>STRING_VAR</var>
                singular_var_node.text = match.group(3).strip()

                # Switch the order of the arguments to match the new signature
                # that is used by plural_form(STRING, NUM)
                plural_var_node.text = (pluralize %
                    (match.group(3).strip(), match.group(2).strip()))

                # Insert the new node after the <var>
                var_node.addnext(singular_var_node)
                cloned_var.addnext(plural_var_node)

                # Insert a space between the two <var>s
                var_node.tail = cloned_var.tail = ' '

                # Handle the special case where we have a pluralTex, we need to
                # wrap the <var> elements in a <code> element
                if match.group(1).strip() == 'pluralTex':
                    code_var_node = var_node.makeelement('code')
                    code_cloned_var = cloned_var.makeelement('code')

                    # Set the tail text to be the same as the <var>s
                    code_var_node.tail = var_node.tail
                    code_cloned_var.tail = cloned_var.tail

                    # Remove the tail text of the <var>s
                    var_node.tail = cloned_var.tail = ''

                    # Insert the <code> elements before the <var>s
                    var_node.addprevious(code_var_node)
                    cloned_var.addprevious(code_cloned_var)

                    # Insert the <var>s into the container <code>
                    code_var_node.append(var_node)
                    code_cloned_var.append(cloned_var)

            # Number is in the second position, this just outputs the plural
            # form of the word depending upon the number. This is what we want
            # so we just convert the usage of plural() to plural_form().
            else:
                var_node.text = match.group(3).strip()
                cloned_var.text = (pluralize %
                    (match.group(2).strip(), match.group(3).strip()))

    def get_condition(self, key):
        """Generates a data-if condition to handle the plural toggle.

        This will turn a string like:
            <p>I have <var>plural(NUM, "cat")</var>.</p>
        Into the following:
            <p data-if="isSingular(NUM)">I have 1 cat.</p>
            <p data-else>I have <var>NUM</var> cats.</p>
        """
        return self._ngetpos_condition % key


class TernaryFilter(IfElseFilter):
    """Switch inline ternary usage to block-level if/else statements.

    One idiom that's used frequently in exercises is that of inline ternary
    statements, which is similar to plural() usage, for example:
        <p>He gave <var>NUM == 1 ? "an apple" : "many apples"</var>.</p>

    We specifically look for the case where there's an condition followed by
    one-or-two strings and convert it into two blocks, for example:
        <p data-if="NUM == 1">He gave an apple.</p>
        <p data-else>He gave many apples.</p>

    In the case where the expression only has a single string usage it greatly
    simplifies the result, for example:
        <p>He gave <var>NUM == 1 ? "an apple" : APPLES</var>.</p>

    Would become:
        <p data-if="NUM == 1">He gave an apple.</p>
        <p data-else>He gave <var>APPLES</var>.</p>

    This creates two strings that have consistent plural usage and are easier
    to translate as a result.
    """
    # Matches: EXPR ? STATEMENT : STATEMENT
    _regex = re.compile(r'^\s*([^\?]+)\s*\?\s*([^:]+)\s*:\s*([^\?]+)\s*$')

    xpath = 'contains(text(),"?")'

    def get_match(self, fix_node):
        """Return a match of a string that roughly matches:
            EXPR ? STATEMENT : STATEMENT
        """
        match = self._regex.match(fix_node.text)

        # Only return the match if one of the statements is a string
        if match and (_STRING_RE.match(match.group(2)) or
            _STRING_RE.match(match.group(3))):
            return match

    def extract_key(self, match):
        """Returns the condition in the ternary expression."""
        return match.group(1).strip()

    def filter_var(self, match, var_node):
        """Replace each node with the appropriate statement.

        The first node is replaced with the first statement. If that
        statement is a string then the string directly replaces the <var>.
        Same goes for the second, cloned, node.
        """
        cloned_var = self._get_cloned_var(var_node)

        first_str_match = _STRING_RE.match(match.group(2))
        second_str_match = _STRING_RE.match(match.group(3))

        # If the first item in the ternary expression is a string
        if first_str_match:
            # Then just replace the <var> with that string
            extract_strings.replace_node(var_node, first_str_match.group(1))
        else:
            # Otherwise just turn it into <var>STATEMENT</var>
            var_node.text = match.group(2).strip()

        # If the second item in the ternary expression is a string
        if second_str_match:
            # Then just replace the cloned <var> with that string
            extract_strings.replace_node(cloned_var,
                second_str_match.group(1))
        else:
            # Otherwise just turn it into <var>STATEMENT</var>
            cloned_var.text = match.group(3).strip()

    def get_condition(self, key):
        """Turns the node into two nodes with a data-if/else.

        For example given:
            <p>He gave <var>NUM == 1 ? "an apple" : APPLES</var>.</p>

        It would become:
            <p data-if="NUM == 1">He gave an apple.</p>
            <p data-else>He gave <var>APPLES</var>.</p>
        """
        return key


def get_plural_form(word):
    """Prompt the user for help getting the correct plural form for a word.

    Plain strings are frequently pluralized in exercises (even though the
    result can easily be hardcoded). This function helps with that process
    by taking a singular form of a string and prompting the user to help
    provide the correct plural form of that string.

    By default the user is given a prompt for the plural form of a word
    in the format: word + "s" (since that's the most common pluralization
    form). A user can just hit enter to accept that format or enter another
    pluralization form.

    Returns the plural form of the input word.
    """
    if word not in _PLURAL_FORMS:
        # Need to print the result so that it goes to stdout
        # If no input was provided then we default to: word + 's'
        plural = prompt_user(('What is the plural form of "%s" [%ss]: ' %
            (word, word)), default=word + 's')

        # Cache the plural form for later
        _PLURAL_FORMS[word] = plural

    # Return the plural form of the word
    return _PLURAL_FORMS[word]


def get_plural_num_pos(match):
    """Prompt to user for help in determining which argument to plural()
    is the one that holds the number.

    For example a call such as: `plural(VAR_A,VAR_B)` is ambiguous. It's
    not clear which argument is the one which holds the string and which holds
    the number so we need user input to determine that.

    Returns a number: 1 if the number is in the first position, 2 if it's in
    the second position.
    """
    # Get the entire string that we're checking and use it to cache the result
    plural_str = match.group(0).strip()

    if plural_str not in _PLURAL_NUM_POS:
        # Determine if either of the two arguments is a number
        first_arg_num = _check_plural_arg_is_num(match.group(2).strip())
        second_arg_num = _check_plural_arg_is_num(match.group(3).strip())

        # The results are equal, this shouldn't happen
        if first_arg_num == second_arg_num:
            # If this is the case then we give up and ask the user for help
            first_arg_num = second_arg_num = None

        pos = None

        # If the first argument is a string or if the second argument is
        # a number, then the second argument must be a number
        if first_arg_num is False or second_arg_num is True:
            pos = 2

        # If the second argument is a string or if the first argument is
        # a number, then the first argument must be a number
        elif second_arg_num is False or first_arg_num is True:
            pos = 1

        # Otherwise the case is ambiguous so we should consult the user
        else:
            # Prompt the user for information as to which argument is the one
            # that holds the string.
            # If the user provides no input then we default to the first
            # argument
            pos = prompt_user(('Ambiguous: %s which is the number? ([1] 2) ' %
                plural_str), default=1)

        # Make sure that the number is an integer and not a string
        _PLURAL_NUM_POS[plural_str] = int(pos)

    return _PLURAL_NUM_POS[plural_str]


def get_is_plural_num(match):
    """Prompt to user for help in determining if the argument to plural()
    is a number.

    For example a call such as: `plural(VAR_A)` is ambiguous. It's not clear
    if the input is a number or a string so we need user input to determine
    that.

    Returns True if the argument is a number.
    """
    # Extract the argument from the match
    plural_str = match.group(2).strip()

    if plural_str not in _IS_PLURAL_NUM:
        # Check to see if the argument is a plural using some
        # low-hanging fruit before asking for user input
        holds_num = _check_plural_arg_is_num(plural_str)

        if holds_num is None:
            # Prompt the user for information as to which argument is the one
            # that holds the string.
            holds_num = prompt_user(
                'Ambiguous: Does %s handle a number? (y/[n]) ' % plural_str,
                default='n')

            # If the user provides no input then we default to considering
            # the argument to be a string.
            # Convert the text input into a boolean.
            holds_num = ('y' in holds_num)

        # Cache the result for later
        _IS_PLURAL_NUM[plural_str] = holds_num

    return _IS_PLURAL_NUM[plural_str]


def _check_plural_arg_is_num(plural_arg):
    """Check to see if a string matches the known ways in which a plural
    argument can be a number.

    Returns True if the argument is a number, returns False if the argument
    is a string. Returns None if the case is ambiguous.
    """
    # If the argument is a string, then it's not a number
    if _STRING_RE.match(plural_arg):
        return False

    # If the argument is a function call
    # And it's one of the built-in string functions, then it's not a number
    fn_match = _FUNCTION_RE.match(plural_arg)
    if fn_match and fn_match.group(1) in _functions:
        return False

    # If it users a var that's in our list of known string variables
    for var in _string_vars:
        if var in plural_arg.upper():
            return False

    # If it users a var that's in our list of known number variables
    for var in _num_vars:
        if var in plural_arg.upper():
            return True

    # Otherwise we bail as we don't know the answer
    return None


def prompt_user(prompt, default=''):
    """Utilty for displaying a prompt and getting the results from a user.

    Uses the global SHOW_PROMPT to determine if the prompt should be shown
    to the user or if it should fall back to the specified default.

    Arguments:
        - prompt: A string to display as the user prompt.
        - default: The result string to fall back to if no response is given.

    Returns:
        - A string containing the response from the user.
    """
    result = None

    # Only show a prompt if we want to use the result
    if SHOW_PROMPT:
        # Need to print the prompt so that it goes to stdout
        print prompt

        # Get the input from the user
        result = raw_input()

    # Use the default if no input is provided or if no prompt
    # is wanted (such is the case when run without --fix)
    return default if not result else result


if __name__ == '__main__':
    main()