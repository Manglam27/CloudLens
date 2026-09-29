import 'package:cloud_lens/Pages/check_page.dart';
import 'package:cloud_lens/check_service.dart';
import 'package:flutter/material.dart';

/// Lists the user's past credibility checks, newest first (FR 1.6).
/// Opening one shows the stored result; the check is not run again.
class HistoryPage extends StatefulWidget {
  const HistoryPage({super.key});

  @override
  State<HistoryPage> createState() => _HistoryPageState();
}

class _HistoryPageState extends State<HistoryPage> {
  List<CheckResult>? _checks;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final checks = await CheckService.history();
      if (mounted) {
        setState(() {
          _checks = checks;
          _error = null;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  String _date(DateTime? d) {
    if (d == null) return '';
    String two(int n) => n.toString().padLeft(2, '0');
    return '${d.year}-${two(d.month)}-${two(d.day)} ${two(d.hour)}:${two(d.minute)}';
  }

  String _title(CheckResult c) {
    if (c.isFailed) return 'Check failed';
    if (!c.isDone) return 'Checking…';
    return c.claim ?? 'No checkable claim found';
  }

  @override
  Widget build(BuildContext context) {
    final checks = _checks;
    Widget body;
    if (_error != null) {
      body = ListView(children: [Padding(padding: const EdgeInsets.all(24), child: Text(_error!))]);
    } else if (checks == null) {
      body = const Center(child: CircularProgressIndicator());
    } else if (checks.isEmpty) {
      body = ListView(children: const [
        Padding(
          padding: EdgeInsets.all(24),
          child: Text('No checks yet. Open a photo in the Cloud tab and tap “Check”.', textAlign: TextAlign.center),
        ),
      ]);
    } else {
      body = ListView.separated(
        itemCount: checks.length,
        separatorBuilder: (_, __) => const Divider(height: 1),
        itemBuilder: (context, i) {
          final c = checks[i];
          return ListTile(
            leading: c.isDone
                ? Verdict.pill(c.verdict, fontSize: 11)
                : Icon(c.isFailed ? Icons.error_outline : Icons.hourglass_top, color: Colors.grey.shade600),
            title: Text(_title(c), maxLines: 2, overflow: TextOverflow.ellipsis),
            subtitle: Text(_date(c.createdAt)),
            onTap: () async {
              await Navigator.push(
                context,
                MaterialPageRoute(builder: (_) => CheckPage(checkId: c.checkId)),
              );
              _load();
            },
          );
        },
      );
    }
    return RefreshIndicator(onRefresh: _load, child: body);
  }
}
