class PluginNotFound(RuntimeError):
    def __init__(self, plugin_name):
        msg = "Plugin {} not found in update center".format(plugin_name)
        super(PluginNotFound, self).__init__(msg)
