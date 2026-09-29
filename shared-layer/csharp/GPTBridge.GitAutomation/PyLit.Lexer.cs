namespace GPTBridge.GitAutomation;

internal static partial class PyLit
{
    private sealed class Lexer
    {
        private readonly string _src;
        private int _pos;
        public Tk Kind;
        public string Text = "";
        public Lexer(string src) { _src = src; Next(); }
        private int _tokStart;
        /// <summary>Position of the current token — restore re-lexes
        /// the current token itself (kw-arg look-ahead).</summary>
        public int Mark() => _tokStart;
        public void Restore(int mark) { _pos = mark; Next(); }
        public string Source => _src;
        public int TokenStart => _tokStart;
        public void Next()
        {
            for (;;)
            {
                if (_pos >= _src.Length) { Kind = Tk.End; return; }
                var c = _src[_pos];
                if (c == '#')
                {
                    while (_pos < _src.Length && _src[_pos] != '\n') _pos++;
                    continue;
                }
                if (char.IsWhiteSpace(c)) { _pos++; continue; }
                if (c == '\\' && _pos + 1 < _src.Length && _src[_pos + 1] == '\n')
                { _pos += 2; continue; }
                break;
            }
            _tokStart = _pos;
            var ch = _src[_pos];
            if (ch == '"' || ch == '\''
                || (ch is 'r' or 'b' or 'f' or 'u' or 'R' or 'B' or 'F' or 'U'
                    && _pos + 1 < _src.Length
                    && (_src[_pos + 1] == '"' || _src[_pos + 1] == '\'')))
            {
                var raw = ch is 'r' or 'R';
                var fstring = ch is 'f' or 'F';
                if (raw || fstring || ch is 'b' or 'B' or 'u' or 'U')
                    _pos++;
                var quote = _src[_pos];
                var triple = _pos + 2 < _src.Length
                    && _src[_pos + 1] == quote && _src[_pos + 2] == quote;
                var delim = triple ? new string(quote, 3) : quote.ToString();
                _pos += delim.Length;
                var end = _src.IndexOf(delim, _pos, StringComparison.Ordinal);
                if (end < 0) { Kind = Tk.End; return; }
                var body = _src[_pos..end];
                _pos = end + delim.Length;
                Kind = Tk.Str;
                Text = raw || fstring
                    ? body
                    : body.Replace("\\n", "\n").Replace("\\t", "\t")
                        .Replace("\\r", "\r").Replace("\\\"", "\"")
                        .Replace("\\'", "'").Replace("\\\\", "\\")
                        .Replace("\\u", "");
                if (fstring) Text = "\0fstring\0" + Text;
                return;
            }
            if (char.IsDigit(ch))
            {
                var start = _pos;
                while (_pos < _src.Length
                       && (char.IsLetterOrDigit(_src[_pos])
                           || _src[_pos] is '.' or '_'))
                    _pos++;
                Kind = Tk.Num;
                Text = _src[start.._pos];
                return;
            }
            if (char.IsLetter(ch) || ch == '_')
            {
                var start = _pos;
                while (_pos < _src.Length
                       && (char.IsLetterOrDigit(_src[_pos])
                           || _src[_pos] == '_'))
                    _pos++;
                Kind = Tk.Name;
                Text = _src[start.._pos];
                return;
            }
            if (ch == '=' && _pos + 1 < _src.Length && _src[_pos + 1] == '=')
            { Kind = Tk.Punct; Text = "=="; _pos += 2; return; }
            if (ch == '-' && _pos + 1 < _src.Length && _src[_pos + 1] == '>')
            { Kind = Tk.Punct; Text = "->"; _pos += 2; return; }
            if (ch == '.' && _pos + 2 < _src.Length
                && _src[_pos + 1] == '.' && _src[_pos + 2] == '.')
            { Kind = Tk.Punct; Text = "..."; _pos += 3; return; }
            Kind = Tk.Punct;
            Text = ch.ToString();
            _pos++;
        }
    }
}
